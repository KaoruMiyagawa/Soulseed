"""Bounded webpage reading, MediaWiki fallback and transport error taxonomy."""

import asyncio
import json
import socket
import ssl
from urllib.parse import parse_qs, urlsplit

import pytest

from app.web_research import (
    HttpResponse,
    PublicHttpFetcher,
    SafeWebPageReader,
    WebResearchError,
    is_page_relevant_to_topic,
)


ARTICLE_URL = "https://en.wikipedia.org/wiki/Whale_vocalization"


class ControlledFetcher:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def fetch(self, url, *, accept="text/html,text/plain;q=0.9"):
        self.calls.append((url, accept))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def mediawiki_response(url= "https://en.wikipedia.org/w/api.php"):
    payload = {
        "query": {
            "pages": [{
                "title": "Whale vocalization",
                "extract": (
                    "Whale vocalization includes calls and songs. Humpback whale songs are "
                    "studied with hydrophones and can be used in communication and courtship. "
                    "Researchers compare recordings across locations and seasons."
                ),
            }]
        }
    }
    query = "action=query&format=json&prop=extracts"
    return HttpResponse(
        f"{url}?{query}", 200, {"content-type": "application/json; charset=utf-8"},
        json.dumps(payload).encode("utf-8"),
    )


def test_large_wikipedia_article_uses_bounded_official_extract_as_readable_evidence():
    fetcher = ControlledFetcher([
        WebResearchError("response_too_large", "large article"),
        mediawiki_response(),
    ])
    page = asyncio.run(SafeWebPageReader(fetcher).read(ARTICLE_URL))

    assert len(fetcher.calls) == 2
    assert fetcher.calls[0] == (ARTICLE_URL, "text/html,text/plain;q=0.9")
    api_url, accept = fetcher.calls[1]
    parsed = urlsplit(api_url)
    params = parse_qs(parsed.query)
    assert parsed.scheme == "https" and parsed.hostname == "en.wikipedia.org"
    assert parsed.path == "/w/api.php" and accept == "application/json"
    assert params["exchars"] == ["8000"]
    assert params["explaintext"] == ["1"]
    assert params["titles"] == ["Whale vocalization"]
    assert page.url == ARTICLE_URL
    assert page.title == "Whale vocalization"
    assert is_page_relevant_to_topic(
        "鲸鱼为什么唱歌？", "whale song humpback communication research", page.title, page.text
    )


def test_large_non_wikipedia_page_remains_rejected_without_fallback():
    fetcher = ControlledFetcher([WebResearchError("response_too_large", "large article")])
    with pytest.raises(WebResearchError) as error:
        asyncio.run(SafeWebPageReader(fetcher).read("https://example.org/article"))
    assert error.value.code == "response_too_large"
    assert len(fetcher.calls) == 1


def test_large_page_only_falls_back_for_exact_wikipedia_hosts_and_article_paths():
    for url in (
        "https://en.wikipedia.org.evil.example/wiki/Whale_vocalization",
        "https://en.wikipedia.org/w/index.php?title=Whale_vocalization",
        "https://zh.wikipedia.org/wiki/Whale_vocalization?oldid=123",
    ):
        fetcher = ControlledFetcher([WebResearchError("response_too_large", "large article")])
        with pytest.raises(WebResearchError) as error:
            asyncio.run(SafeWebPageReader(fetcher).read(url))
        assert error.value.code == "response_too_large"
        assert len(fetcher.calls) == 1


def test_mediawiki_api_must_remain_on_authorized_official_host_and_return_json():
    responses = [
        HttpResponse("https://attacker.example/w/api.php", 200,
                     {"content-type": "application/json"}, b"{}"),
        HttpResponse("https://en.wikipedia.org/w/api.php", 200,
                     {"content-type": "text/html"}, b"<html></html>"),
        HttpResponse("https://en.wikipedia.org/w/api.php", 200,
                     {"content-type": "application/json"}, b"not-json"),
    ]
    expected = ["mediawiki_api_unavailable", "mediawiki_api_invalid", "mediawiki_api_invalid"]
    for response, code in zip(responses, expected, strict=True):
        fetcher = ControlledFetcher([WebResearchError("response_too_large", "large article"), response])
        with pytest.raises(WebResearchError) as error:
            asyncio.run(SafeWebPageReader(fetcher).read(ARTICLE_URL))
        assert error.value.code == code


def test_regular_html_extracts_article_text_and_ignores_navigation_and_script():
    html = (
        "<html><head><title>Whale article</title></head><body>"
        "<nav>Home Menu Navigation links</nav><script>fake hidden whale facts</script>"
        "<main><article><h1>Whale vocalization</h1><p>Humpback whale songs are recorded "
        "with hydrophones. Researchers compare calls to study communication and courtship.</p>"
        "</article></main></body></html>"
    ).encode()
    fetcher = ControlledFetcher([HttpResponse(
        ARTICLE_URL, 200, {"content-type": "text/html; charset=utf-8"}, html,
    )])
    page = asyncio.run(SafeWebPageReader(fetcher).read(ARTICLE_URL))
    assert page.title == "Whale article"
    assert "Humpback whale songs" in page.text
    assert "Navigation links" not in page.text and "fake hidden" not in page.text
    assert is_page_relevant_to_topic(
        "whale song", "humpback whale song research", page.title, page.text
    )


def _public_resolver(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def test_transport_errors_distinguish_tls_target_and_proxy_failures():
    async def tls_failure(*args, **kwargs):
        raise ssl.SSLCertVerificationError("certificate verification failed")

    tls_fetcher = PublicHttpFetcher(resolver=_public_resolver, opener=tls_failure, proxy=None)
    with pytest.raises(WebResearchError) as tls_error:
        asyncio.run(tls_fetcher.fetch("https://example.org/article"))
    assert tls_error.value.code == "tls_handshake_failed"

    async def refused(*args, **kwargs):
        raise ConnectionRefusedError("refused")

    target_fetcher = PublicHttpFetcher(resolver=_public_resolver, opener=refused, proxy=None)
    with pytest.raises(WebResearchError) as target_error:
        asyncio.run(target_fetcher.fetch("http://example.org/article"))
    assert target_error.value.code == "connect_failed"

    proxy_fetcher = PublicHttpFetcher(
        resolver=_public_resolver, opener=refused, proxy="http://127.0.0.1:7897"
    )
    with pytest.raises(WebResearchError) as proxy_error:
        asyncio.run(proxy_fetcher.fetch("https://example.org/article"))
    assert proxy_error.value.code == "proxy_connection_failed"


def test_dns_failure_stays_distinct_from_connect_and_http_status_failures():
    def failed_resolver(host, port, **kwargs):
        raise socket.gaierror("name lookup failed")

    fetcher = PublicHttpFetcher(resolver=failed_resolver, opener=None, proxy=None)
    with pytest.raises(WebResearchError) as error:
        asyncio.run(fetcher.fetch("https://example.org/article"))
    assert error.value.code == "dns_failed"


def test_content_length_over_limit_is_rejected_before_reading_body():
    fetcher = PublicHttpFetcher(max_response_bytes=32, proxy=None)

    class UnusedReader:
        async def readexactly(self, _size):
            raise AssertionError("oversized body must not be read")

    with pytest.raises(WebResearchError) as error:
        asyncio.run(fetcher._read_body(UnusedReader(), {"content-length": "33"}))
    assert error.value.code == "response_too_large"


def test_page_read_timeout_and_total_fetch_timeout_remain_distinct(monkeypatch):
    import app.web_research as web

    class Writer:
        def write(self, _data): pass
        async def drain(self): pass
        def close(self): pass
        async def wait_closed(self): pass

    async def hanging_opener(*args, **kwargs):
        return asyncio.StreamReader(), Writer()

    def direct_resolver(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(web, "READ_TIMEOUT_SECONDS", 0.01)
    read_fetcher = PublicHttpFetcher(resolver=direct_resolver, opener=hanging_opener, proxy=None)
    with pytest.raises(WebResearchError) as read_error:
        asyncio.run(read_fetcher.fetch("http://example.org/article"))
    assert read_error.value.code == "read_timeout"

    async def slow_opener(*args, **kwargs):
        await asyncio.sleep(1)

    total_fetcher = PublicHttpFetcher(opener=slow_opener, proxy=None, total_timeout=0.01)
    with pytest.raises(WebResearchError) as total_error:
        asyncio.run(total_fetcher.fetch("https://93.184.216.34/article"))
    assert total_error.value.code == "fetch_timeout"


def test_target_http_refusal_is_not_reported_as_dns_or_connection_failure():
    class Reader(asyncio.StreamReader):
        def __init__(self):
            super().__init__()
            self.feed_data(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            self.feed_eof()

    class Writer:
        def write(self, _data): pass
        async def drain(self): pass
        def close(self): pass
        async def wait_closed(self): pass

    async def refused_by_site(*args, **kwargs):
        return Reader(), Writer()

    fetcher = PublicHttpFetcher(resolver=_public_resolver, opener=refused_by_site, proxy=None)
    with pytest.raises(WebResearchError, match="HTTP 403") as error:
        asyncio.run(fetcher.fetch("http://example.org/article"))
    assert error.value.code == "http_status"
