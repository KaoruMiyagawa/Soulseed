"""Read-only public web search and webpage access for bounded exploration."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import ssl
import urllib.parse
import urllib.request
import certifi
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit, urlunsplit


MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 1_000_000
MAX_PAGE_TEXT_CHARS = 8_000
MAX_SEARCH_RESULTS = 5
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 10
TOTAL_FETCH_TIMEOUT_SECONDS = 15
MAX_RESPONSE_HEADERS_BYTES = 32_768
RESEARCH_USER_AGENT = "SoulseedResearchBot/1.0 (https://github.com/KaoruMiyagawa/Soulseed)"
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
DOH_SERVER = "cloudflare-dns.com"
DOH_SERVER_IP = ipaddress.ip_address("1.1.1.1")
_AUTO_PROXY = object()


class WebResearchError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    provider: str | None = None
    relevant: bool | None = None


_LATIN_STOPWORDS = {"the", "and", "for", "with", "from", "what", "why", "how", "does", "are"}
_CJK_STOP_TERMS = ("为什么", "为何", "科学家", "如何", "怎样", "怎么", "什么", "原因", "方法", "研究", "介绍", "请问")
_CJK_STOP_BIGRAMS = {"为什么", "为何", "科学", "学家", "如何", "怎样", "怎么", "什么", "原因", "方法", "研究", "介绍", "请问"}
_SIMPLIFIED_TO_TRADITIONAL = str.maketrans({
    "鲸": "鯨", "鱼": "魚", "声": "聲", "学": "學", "国": "國", "体": "體",
    "听": "聽", "观": "觀", "风": "風", "长": "長", "海": "海",
})


def search_query_terms(query: str) -> set[str]:
    """Extract conservative, language-neutral anchors for result relevance checks."""
    terms: set[str] = set()
    for word in re.findall(r"[a-z0-9]+", query.casefold()):
        if len(word) >= 3 and word not in _LATIN_STOPWORDS:
            terms.add(word)
    for run in re.findall(r"[\u3400-\u9fff]+", query):
        cleaned = run
        for stop in _CJK_STOP_TERMS:
            cleaned = cleaned.replace(stop, " ")
        for phrase in re.findall(r"[\u3400-\u9fff]+", cleaned):
            if len(phrase) == 2:
                terms.add(phrase)
            elif len(phrase) > 2:
                terms.update(
                    phrase[index:index + 2]
                    for index in range(len(phrase) - 1)
                    if phrase[index:index + 2] not in _CJK_STOP_BIGRAMS
                )
    return terms


def is_search_result_relevant(query: str, title: str, snippet: str) -> bool:
    terms = search_query_terms(query)
    if not terms:
        return False
    content = f"{title} {snippet}".casefold()
    traditional_content = content.translate(_SIMPLIFIED_TO_TRADITIONAL)
    matches = {
        term for term in terms
        if term.casefold() in content or term.translate(_SIMPLIFIED_TO_TRADITIONAL).casefold() in traditional_content
    }
    # A result must match a topic noun, not just generic words such as
    # "research", "sound", or "method". Take anchors from the query itself
    # so this works across subjects without a topic-specific allowlist.
    anchors = _topic_anchors(query, terms)
    if anchors and not any(
        anchor.casefold() in content
        or anchor.translate(_SIMPLIFIED_TO_TRADITIONAL).casefold() in traditional_content
        for anchor in anchors
    ):
        return False
    threshold = 2 if len(terms) >= 3 else 1
    return len(matches) >= threshold


def is_page_relevant(query: str, title: str, text: str) -> bool:
    """Check the retrieved page body as well as its title before using it as evidence."""
    terms = search_query_terms(query)
    if not terms or not text:
        return False
    # A matching headline alone is not evidence: disambiguation, redirects, or
    # thin pages can have an on-topic title with unrelated body text.
    content = text[:MAX_PAGE_TEXT_CHARS].casefold()
    traditional_content = content.translate(_SIMPLIFIED_TO_TRADITIONAL)
    matches = {
        term for term in terms
        if term.casefold() in content or term.translate(_SIMPLIFIED_TO_TRADITIONAL).casefold() in traditional_content
    }
    anchors = _topic_anchors(query, terms)
    if anchors and not any(
        anchor.casefold() in content
        or anchor.translate(_SIMPLIFIED_TO_TRADITIONAL).casefold() in traditional_content
        for anchor in anchors
    ):
        return False
    return len(matches) >= (2 if len(terms) >= 3 else 1)


def _topic_anchors(query: str, terms: set[str]) -> set[str]:
    generic = {
        "研究", "如何", "原因", "方法", "功能", "声音", "聲音", "科学", "科學", "学家", "學家",
        "介绍", "介紹", "资料", "資料", "相关", "相關", "信息", "資訊", "声音", "声学", "聲學",
        "study", "research", "method", "methods", "sound", "sounds", "acoustic", "acoustics",
        "why", "how", "what", "cause", "causes", "scientist", "scientists",
    }
    return {term for term in terms if term.casefold() not in generic}


def _tag_search_results(query: str, rows: list[SearchResult], provider: str) -> list[SearchResult]:
    tagged = [
        SearchResult(row.title, row.url, row.snippet, provider,
                     is_search_result_relevant(query, row.title, row.snippet))
        for row in rows
    ]
    return sorted(tagged, key=lambda result: result.relevant is not True)


def _english_query(query: str) -> str:
    terms = [word for word in re.findall(r"[a-z0-9]+", query.casefold())
             if len(word) >= 3 and word not in _LATIN_STOPWORDS]
    return " ".join(dict.fromkeys(terms))


@dataclass(frozen=True)
class ReadPage:
    title: str
    url: str
    retrieved_at: str
    text: str


class PublicHttpFetcher:
    """HTTP/1.1 GET pinned to a validated public DNS answer.

    Connecting to the validated IP directly prevents a second DNS lookup from
    rebinding the host to a private address. HTTPS still uses the original host
    for SNI and certificate verification. Redirects are validated independently.
    """

    def __init__(self, *, resolver=socket.getaddrinfo, opener=asyncio.open_connection,
                 proxy=_AUTO_PROXY,
                 max_redirects: int = MAX_REDIRECTS, max_response_bytes: int = MAX_RESPONSE_BYTES,
                 total_timeout: float = TOTAL_FETCH_TIMEOUT_SECONDS):
        self.resolver = resolver
        self.opener = opener
        self.proxy = self._discover_proxy() if proxy is _AUTO_PROXY else self._validate_proxy(proxy)
        self.max_redirects = max_redirects
        self.max_response_bytes = max_response_bytes
        self.total_timeout = total_timeout
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    @staticmethod
    def _discover_proxy() -> tuple[str, int] | None:
        """Use an existing local HTTP system proxy without forwarding credentials."""
        proxies = urllib.request.getproxies()
        raw = proxies.get("https") or proxies.get("http") or proxies.get("all")
        return PublicHttpFetcher._validate_proxy(raw) if raw else None

    @staticmethod
    def _validate_proxy(raw) -> tuple[str, int] | None:
        if raw is None:
            return None
        try:
            parsed = urllib.parse.urlsplit(raw if "://" in raw else "http://" + raw)
            port = parsed.port
        except (TypeError, ValueError) as exc:
            raise WebResearchError("proxy_unavailable", "系统 HTTP 代理配置无效") from exc
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError as exc:
            raise WebResearchError("proxy_unavailable", "网页读取只允许使用无凭据的本机 HTTP 代理") from exc
        if parsed.scheme.casefold() != "http" or not address.is_loopback or not port or parsed.username or parsed.password:
            raise WebResearchError("proxy_unavailable", "网页读取只允许使用无凭据的本机 HTTP 代理")
        return str(address), port

    async def fetch(self, url: str, *, accept: str = "text/html,text/plain;q=0.9") -> HttpResponse:
        current = self._validate_url_syntax(url)
        visited: set[str] = set()
        try:
            async with asyncio.timeout(self.total_timeout):
                for redirect_count in range(self.max_redirects + 1):
                    if current in visited:
                        raise WebResearchError("redirect_loop", "网页跳转形成循环")
                    visited.add(current)
                    parsed, address = await self._resolve_public_target(current)
                    response = await self._request_once(current, parsed, address, accept=accept)
                    if response.status == 202 and parsed.hostname == "html.duckduckgo.com":
                        body = _decode_body(response).casefold()
                        if "anomaly" in body:
                            raise WebResearchError("search_challenge", "DuckDuckGo 暂时要求人工验证")
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise WebResearchError("invalid_redirect", "网页跳转缺少目标地址")
                        if redirect_count >= self.max_redirects:
                            raise WebResearchError("too_many_redirects", "网页跳转次数超过限制")
                        current = self._validate_url_syntax(urljoin(current, location))
                        continue
                    if not 200 <= response.status < 300:
                        raise WebResearchError("http_status", f"网页返回 HTTP {response.status}")
                    return response
        except TimeoutError as exc:
            raise WebResearchError("fetch_timeout", "网页读取超过总时限") from exc
        raise WebResearchError("fetch_timeout", "网页读取超过总时限")

    @staticmethod
    def _validate_url_syntax(url: str) -> str:
        if not isinstance(url, str) or not url or len(url) > 2_000:
            raise WebResearchError("invalid_url", "网页地址无效")
        if any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise WebResearchError("invalid_url", "网页地址包含控制字符")
        try:
            parsed = urlsplit(url.strip())
            scheme = parsed.scheme.casefold()
            if scheme not in {"http", "https"}:
                raise WebResearchError("unsafe_url", "只允许 HTTP 或 HTTPS 网页")
            if parsed.username is not None or parsed.password is not None:
                raise WebResearchError("unsafe_url", "网页地址不能包含登录凭据")
            host = parsed.hostname
            if not host or "%" in host:
                raise WebResearchError("invalid_url", "网页地址缺少有效主机名")
            port = parsed.port or (443 if scheme == "https" else 80)
            if port not in {80, 443} or (scheme == "https" and port != 443) or (scheme == "http" and port != 80):
                raise WebResearchError("unsafe_url", "只允许网页标准端口")
            try:
                ascii_host = host.encode("idna").decode("ascii").casefold().rstrip(".")
            except UnicodeError as exc:
                raise WebResearchError("invalid_url", "网页主机名无效") from exc
            if ascii_host in {"localhost", "metadata.google.internal"} or ascii_host.endswith((".localhost", ".local", ".internal", ".test", ".home.arpa")):
                raise WebResearchError("unsafe_url", "不允许访问本机或内部主机名")
            display_host = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
            netloc = display_host if port == (443 if scheme == "https" else 80) else f"{display_host}:{port}"
            path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
            query = quote(parsed.query, safe="/?@:!$&'()*+,;=-._~%")
            return urlunsplit((scheme, netloc, path, query, ""))
        except ValueError as exc:
            raise WebResearchError("invalid_url", "网页地址格式无效") from exc

    async def _resolve_public_target(self, url: str):
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            try:
                records = await asyncio.wait_for(
                    asyncio.to_thread(self.resolver, host, port, type=socket.SOCK_STREAM),
                    timeout=CONNECT_TIMEOUT_SECONDS,
                )
            except (OSError, TimeoutError) as exc:
                raise WebResearchError("dns_failed", "网页主机名解析失败") from exc
            addresses = []
            for record in records:
                try:
                    address = ipaddress.ip_address(record[4][0])
                except (IndexError, ValueError, TypeError):
                    continue
                if address not in addresses:
                    addresses.append(address)
            if addresses and all(address in FAKE_IP_NETWORK for address in addresses):
                addresses = await self._resolve_with_doh(host)
            if not addresses or any(not self._is_public(address) for address in addresses):
                raise WebResearchError("unsafe_address", "网页主机名解析到非公网地址")
            literal = addresses[0]
        if not self._is_public(literal):
            raise WebResearchError("unsafe_address", "不允许访问本机、局域网或受保护地址")
        return parsed, literal

    async def _resolve_with_doh(self, host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        """Resolve Fake-IP names over verified DoH, then pin the result in CONNECT."""
        addresses = []
        for record_type in ("A", "AAAA"):
            query = urllib.parse.urlencode({"name": host, "type": record_type})
            url = f"https://{DOH_SERVER}/dns-query?{query}"
            parsed = urlsplit(url)
            response = await self._request_once(
                url, parsed, DOH_SERVER_IP, accept="application/dns-json"
            )
            if response.status != 200 or len(response.body) > 32_768:
                raise WebResearchError("dns_failed", "安全 DNS 查询失败")
            if "application/dns-json" not in response.headers.get("content-type", "").casefold():
                raise WebResearchError("dns_failed", "安全 DNS 响应格式无效")
            try:
                payload = json.loads(response.body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WebResearchError("dns_failed", "安全 DNS 响应格式无效") from exc
            if not isinstance(payload, dict) or payload.get("Status") not in {0, 3}:
                raise WebResearchError("dns_failed", "安全 DNS 查询失败")
            for answer in payload.get("Answer", []):
                if not isinstance(answer, dict) or answer.get("type") not in {1, 28}:
                    continue
                try:
                    address = ipaddress.ip_address(answer.get("data", ""))
                except ValueError as exc:
                    raise WebResearchError("dns_failed", "安全 DNS 返回了无效地址") from exc
                if ((answer["type"] == 1 and address.version != 4)
                        or (answer["type"] == 28 and address.version != 6)):
                    raise WebResearchError("dns_failed", "安全 DNS 返回的地址类型无效")
                if address not in addresses:
                    addresses.append(address)
        if not addresses or any(not self._is_public(address) for address in addresses):
            raise WebResearchError("unsafe_address", "安全 DNS 返回了非公网地址")
        return addresses

    @staticmethod
    def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            return address.ipv4_mapped.is_global
        return address.is_global and not address.is_multicast

    async def _request_once(self, url: str, parsed, address: ipaddress.IPv4Address | ipaddress.IPv6Address,
                            *, accept: str = "text/html,text/plain;q=0.9") -> HttpResponse:
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        use_tls = parsed.scheme == "https"
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                reader, writer = await self._open_pinned(address, port, use_tls, host)
        except (OSError, TimeoutError, ssl.SSLError) as exc:
            raise WebResearchError("connect_failed", "无法连接目标网页") from exc

        try:
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            host_header = f"[{host}]" if ":" in host else host
            if port != (443 if use_tls else 80):
                host_header = f"{host_header}:{port}"
            request = (
                f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\n"
                f"User-Agent: {RESEARCH_USER_AGENT}\r\nAccept: {accept}\r\n"
                "Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            writer.write(request)
            async with asyncio.timeout(READ_TIMEOUT_SECONDS):
                await writer.drain()
                status_line = await reader.readline()
                if len(status_line) > 8_192 or not status_line:
                    raise WebResearchError("invalid_response", "网页响应状态行无效")
                try:
                    version, status_text, _ = status_line.decode("ascii").rstrip("\r\n").split(" ", 2)
                    status = int(status_text)
                except (ValueError, UnicodeError) as exc:
                    raise WebResearchError("invalid_response", "网页响应状态无效") from exc
                if version not in {"HTTP/1.0", "HTTP/1.1"}:
                    raise WebResearchError("invalid_response", "网页响应协议无效")
                headers: dict[str, str] = {}
                header_size = 0
                while True:
                    line = await reader.readline()
                    header_size += len(line)
                    if header_size > MAX_RESPONSE_HEADERS_BYTES or not line:
                        raise WebResearchError("response_too_large", "网页响应头超过限制")
                    if line in {b"\r\n", b"\n"}:
                        break
                    try:
                        key, value = line.decode("latin-1").split(":", 1)
                    except ValueError as exc:
                        raise WebResearchError("invalid_response", "网页响应头无效") from exc
                    name = key.strip().casefold()
                    value = value.strip()
                    if name in headers and name in {"content-length", "transfer-encoding", "location"}:
                        raise WebResearchError("invalid_response", "网页响应包含冲突的响应头")
                    headers[name] = f"{headers[name]}, {value}" if name in headers else value
                body = await self._read_body(reader, headers)
                return HttpResponse(url=url, status=status, headers=headers, body=body)
        except TimeoutError as exc:
            raise WebResearchError("read_timeout", "等待网页响应超过时限") from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass

    async def _open_pinned(self, address, port: int, use_tls: bool, host: str):
        """Connect to an exact validated IP, through the configured proxy if present."""
        if self.proxy is None:
            return await self.opener(
                str(address), port,
                ssl=self.ssl_context if use_tls else None,
                server_hostname=host if use_tls else None,
            )

        proxy_host, proxy_port = self.proxy
        reader, writer = await self.opener(proxy_host, proxy_port)
        target = f"[{address}]" if address.version == 6 else str(address)
        try:
            writer.write(
                (f"CONNECT {target}:{port} HTTP/1.1\r\nHost: {target}:{port}\r\n"
                 "Proxy-Connection: Keep-Alive\r\n\r\n").encode("ascii")
            )
            await writer.drain()
            async with asyncio.timeout(READ_TIMEOUT_SECONDS):
                status_line = await reader.readline()
                if len(status_line) > 8_192:
                    raise WebResearchError("proxy_connect_failed", "本机代理响应无效")
                parts = status_line.decode("latin-1", errors="replace").strip().split(" ", 2)
                if len(parts) < 2 or parts[0] not in {"HTTP/1.0", "HTTP/1.1"} or parts[1] != "200":
                    raise WebResearchError("proxy_connect_failed", "本机代理拒绝固定目标连接")
                header_size = 0
                while True:
                    line = await reader.readline()
                    header_size += len(line)
                    if not line or header_size > MAX_RESPONSE_HEADERS_BYTES:
                        raise WebResearchError("proxy_connect_failed", "本机代理响应头无效")
                    if line in {b"\r\n", b"\n"}:
                        break
            if use_tls:
                await writer.start_tls(self.ssl_context, server_hostname=host)
            return reader, writer
        except BaseException:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass
            raise

    async def _read_body(self, reader, headers: dict[str, str]) -> bytes:
        encoding = headers.get("content-encoding", "identity").casefold()
        if encoding not in {"", "identity"}:
            raise WebResearchError("unsupported_encoding", "网页使用了不支持的压缩格式")
        transfer = headers.get("transfer-encoding", "").casefold()
        if transfer:
            if transfer != "chunked":
                raise WebResearchError("invalid_response", "网页传输编码无效")
            output = bytearray()
            while True:
                line = await reader.readline()
                if len(line) > 128:
                    raise WebResearchError("invalid_response", "网页分块长度无效")
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError as exc:
                    raise WebResearchError("invalid_response", "网页分块长度无效") from exc
                if size == 0:
                    trailer_size = 0
                    while True:
                        trailer = await reader.readline()
                        trailer_size += len(trailer)
                        if trailer_size > MAX_RESPONSE_HEADERS_BYTES or not trailer:
                            raise WebResearchError("invalid_response", "网页分块尾部无效")
                        if trailer in {b"\r\n", b"\n"}:
                            break
                    break
                if size < 0 or len(output) + size > self.max_response_bytes:
                    raise WebResearchError("response_too_large", "网页内容超过大小限制")
                chunk = await reader.readexactly(size + 2)
                if not chunk.endswith(b"\r\n"):
                    raise WebResearchError("invalid_response", "网页分块格式无效")
                output.extend(chunk[:-2])
            return bytes(output)
        length = headers.get("content-length")
        if length is not None:
            try:
                size = int(length)
            except ValueError as exc:
                raise WebResearchError("invalid_response", "网页内容长度无效") from exc
            if size < 0 or size > self.max_response_bytes:
                raise WebResearchError("response_too_large", "网页内容超过大小限制")
            try:
                return await reader.readexactly(size)
            except asyncio.IncompleteReadError as exc:
                raise WebResearchError("invalid_response", "网页内容未完整返回") from exc
        output = bytearray()
        while len(output) <= self.max_response_bytes:
            chunk = await reader.read(min(65_536, self.max_response_bytes + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
        if len(output) > self.max_response_bytes:
            raise WebResearchError("response_too_large", "网页内容超过大小限制")
        return bytes(output)


def _decode_body(response: HttpResponse) -> str:
    content_type = response.headers.get("content-type", "").casefold()
    if not any(kind in content_type for kind in ("text/html", "application/xhtml+xml", "text/plain")):
        raise WebResearchError("unsupported_content_type", "网页不是可读取的 HTML 或纯文本")
    charset = "utf-8"
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.strip() == "charset" and value.strip():
            candidate = value.strip().strip("\"'")
            try:
                "".encode(candidate)
                charset = candidate
            except LookupError:
                pass
    try:
        return response.body.decode(charset, errors="replace")
    except LookupError:
        return response.body.decode("utf-8", errors="replace")


class _ReadableHTML(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"}

    def __init__(self, max_chars: int):
        super().__init__(convert_charrefs=True)
        self.max_chars = max_chars
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.in_title = False
        self.skip_depth = 0
        self.length = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        if tag in self._SKIP_TAGS:
            self.skip_depth += 1
        elif tag == "title" and not self.skip_depth:
            self.in_title = True
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3", "article", "section"} and not self.skip_depth:
            self._append_text("\n")

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in self._SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "title":
            self.in_title = False
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3", "article", "section"} and not self.skip_depth:
            self._append_text("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        elif not self.skip_depth:
            self._append_text(data)

    def _append_text(self, value: str):
        remaining = self.max_chars - self.length
        if remaining <= 0:
            return
        piece = value[:remaining]
        self.text_parts.append(piece)
        self.length += len(piece)


class _DuckDuckGoResults(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._title: list[str] | None = None
        self._snippet: list[str] | None = None
        self._href = ""
        self._last_result: int | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag.casefold() == "a" and "result__a" in classes:
            self._title = []
            self._href = attributes.get("href") or ""
        elif tag.casefold() == "a" and "result__snippet" in classes:
            self._snippet = []

    def handle_data(self, data):
        if self._title is not None:
            self._title.append(data)
        if self._snippet is not None:
            self._snippet.append(data)

    def handle_endtag(self, tag):
        if tag.casefold() != "a":
            return
        if self._title is not None:
            title = " ".join("".join(self._title).split())
            target = self._unwrap(self._href)
            if title and target:
                self.results.append({"title": title[:240], "url": target, "snippet": ""})
                self._last_result = len(self.results) - 1
            self._title = None
        elif self._snippet is not None:
            if self._last_result is not None:
                self.results[self._last_result]["snippet"] = " ".join("".join(self._snippet).split())[:600]
            self._snippet = None

    @staticmethod
    def _unwrap(href: str) -> str:
        candidate = urljoin("https://duckduckgo.com", href)
        parsed = urlsplit(candidate)
        if parsed.hostname and parsed.hostname.casefold().endswith("duckduckgo.com"):
            redirect = parse_qs(parsed.query).get("uddg", [])
            if redirect:
                candidate = redirect[0]
        try:
            return PublicHttpFetcher._validate_url_syntax(candidate)
        except WebResearchError:
            return ""


class DuckDuckGoSearchProvider:
    """Free, keyless public web search. Network failures are reported as failures."""

    def __init__(self, fetcher: PublicHttpFetcher | None = None):
        # DuckDuckGo HTML is the volatile, non-keyed provider. Bound its wait
        # so a blocked response does not consume almost the whole task budget
        # before the already configured Wikimedia fallback is tried.
        self.fetcher = fetcher or PublicHttpFetcher(max_response_bytes=600_000, total_timeout=6)

    async def search(self, query: str) -> list[SearchResult]:
        if not isinstance(query, str) or not 3 <= len(query.strip()) <= 240:
            raise WebResearchError("invalid_query", "搜索词长度必须在 3 到 240 个字符之间")
        url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query.strip()})
        response = await self.fetcher.fetch(url)
        parser = _DuckDuckGoResults()
        parser.feed(_decode_body(response))
        results: list[SearchResult] = []
        seen: set[str] = set()
        for row in parser.results:
            if row["url"] in seen:
                continue
            seen.add(row["url"])
            results.append(SearchResult(row["title"], row["url"], row["snippet"]))
            if len(results) >= MAX_SEARCH_RESULTS:
                break
        return _tag_search_results(query.strip(), results, "duckduckgo")


class WikimediaSearchProvider:
    """Free MediaWiki search fallback, limited to public encyclopedia articles."""

    def __init__(self, fetcher: PublicHttpFetcher | None = None, *, language: str = "zh"):
        self.fetcher = fetcher or PublicHttpFetcher(max_response_bytes=300_000)
        if language not in {"zh", "en"}:
            raise ValueError("unsupported_wikipedia_language")
        self.language = language

    async def search(self, query: str) -> list[SearchResult]:
        if not isinstance(query, str) or not 3 <= len(query.strip()) <= 240:
            raise WebResearchError("invalid_query", "搜索词长度必须在 3 到 240 个字符之间")
        params = urlencode({
            "action": "query", "list": "search", "srsearch": query.strip(),
            "srnamespace": "0", "srlimit": str(MAX_SEARCH_RESULTS),
            "srprop": "snippet", "format": "json", "formatversion": "2", "utf8": "1",
            "uselang": self.language,
        })
        url = f"https://{self.language}.wikipedia.org/w/api.php?{params}"
        response = await self.fetcher.fetch(url, accept="application/json")
        if "application/json" not in response.headers.get("content-type", "").casefold():
            raise WebResearchError("search_format_changed", "百科搜索返回格式无效")
        try:
            payload = json.loads(response.body)
            rows = payload["query"]["search"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise WebResearchError("search_format_changed", "百科搜索返回格式无效") from exc
        if not isinstance(rows, list):
            raise WebResearchError("search_format_changed", "百科搜索返回格式无效")
        results = []
        for row in rows[:MAX_SEARCH_RESULTS]:
            title = row.get("title") if isinstance(row, dict) else None
            if not isinstance(title, str) or not title.strip():
                continue
            snippet_parser = _ReadableHTML(600)
            snippet_parser.feed(str(row.get("snippet") or ""))
            snippet = " ".join("".join(snippet_parser.text_parts).split())
            article = f"https://{self.language}.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='()_,-')}"
            results.append(SearchResult(title[:240], article, snippet[:600]))
        provider = f"wikimedia_{self.language}"
        return _tag_search_results(query.strip(), results, provider)


class FallbackSearchProvider:
    """Try keyless web search, then the matching public Wikipedia language index."""

    def __init__(self, primary=None, fallback=None, english_fallback=None):
        self.primary = primary or DuckDuckGoSearchProvider()
        self.fallback = fallback or WikimediaSearchProvider(language="zh")
        self.english_fallback = english_fallback or (fallback if fallback is not None
                                                     else WikimediaSearchProvider(language="en"))

    async def search(self, query: str) -> list[SearchResult]:
        primary_error = None
        primary_results: list[SearchResult] = []
        try:
            primary_results = await self.primary.search(query)
        except WebResearchError as exc:
            if exc.code == "invalid_query":
                raise
            primary_error = exc
        if any(result.relevant is True for result in primary_results):
            return primary_results

        english_query = _english_query(query)
        fallback = self.english_fallback if english_query else self.fallback
        fallback_query = english_query or query
        try:
            fallback_results = await fallback.search(fallback_query)
        except WebResearchError as exc:
            if primary_error is not None:
                raise WebResearchError("search_unavailable", "所有已配置的搜索服务均不可用") from exc
            if primary_results:
                return primary_results
            raise
        if any(result.relevant is True for result in fallback_results):
            return fallback_results
        return primary_results or fallback_results


class SafeWebPageReader:
    def __init__(self, fetcher: PublicHttpFetcher | None = None, *, max_text_chars: int = MAX_PAGE_TEXT_CHARS):
        self.fetcher = fetcher or PublicHttpFetcher()
        self.max_text_chars = max(500, min(max_text_chars, MAX_PAGE_TEXT_CHARS))

    async def read(self, url: str) -> ReadPage:
        response = await self.fetcher.fetch(url)
        source = _decode_body(response)
        content_type = response.headers.get("content-type", "").casefold()
        if "text/plain" in content_type:
            title = urlsplit(response.url).hostname or "网页"
            text = " ".join(source.split())[:self.max_text_chars]
        else:
            parser = _ReadableHTML(self.max_text_chars)
            parser.feed(source)
            title = " ".join("".join(parser.title_parts).split())[:240]
            text = " ".join("".join(parser.text_parts).split())[:self.max_text_chars]
        if len(text) < 40:
            raise WebResearchError("insufficient_page_text", "网页没有足够的可读正文")
        return ReadPage(
            title=title or (urlsplit(response.url).hostname or "网页"),
            url=response.url,
            retrieved_at=datetime.now(UTC).isoformat(),
            text=text,
        )
