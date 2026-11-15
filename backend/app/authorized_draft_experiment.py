"""One authorized HTTPS completion into a draft-only report and private SQLite ledger."""

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import uuid4

import httpx

from .autonomous_personas import _catalog_profile
from .draft_experiment import ExperimentCase, ExperimentReport, _CHARACTER, _SAMPLES, _review_model_draft, _sample_messages
from .proactive_fact_review import ReviewStatus


_PREFIX = "SOULSEED_DRAFT_EXPERIMENT_"
_ENDPOINTS = {"https://api.siliconflow.cn/v1/chat/completions"}
_ROOT_NAME = ".soulseed-draft-experiments"
_LEDGER_FILE = "ledger.sqlite3"
_REQUEST_TIMEOUT = 30
_RECOVERY_GRACE = 60
_EXPERIMENT_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash"
_MAX_OUTPUT_TOKENS = 256
_MAX_INPUT_BYTES = 8192
_MAX_RESPONSE_BYTES = 16384
_MAX_GRANT_BYTES = 4096
_MAX_GRANT_AGE = timedelta(minutes=15)


class ExperimentRejected(ValueError):
    """Bounded diagnostic safe for the CLI to display."""


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")


def _utc(clock):
    instant = clock()
    if instant.utcoffset() is None:
        raise ExperimentRejected("experiment_clock_requires_timezone")
    return instant.astimezone(UTC)


def _now():
    return datetime.now(UTC)


def _format_time(instant):
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value):
    try:
        instant = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        if _format_time(instant) == value:
            return instant
    except (ValueError, TypeError):
        pass
    raise ExperimentRejected("invalid_grant_timestamp")


def _run_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}", value):
        raise ExperimentRejected("invalid_experiment_run_id")
    return value


@dataclass(frozen=True)
class _Provider:
    endpoint: str
    model: str

    def __post_init__(self):
        if self.endpoint not in _ENDPOINTS or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", self.model):
            raise ExperimentRejected("experiment_provider_not_allowed")


def _configured_provider():
    provider = _Provider(os.environ.get(_PREFIX + "ENDPOINT", ""), os.environ.get(_PREFIX + "MODEL", ""))
    if provider.model != _EXPERIMENT_MODEL:
        raise ExperimentRejected("experiment_model_not_allowed")
    return provider


def _verification_key():
    value = os.environ.get(_PREFIX + "GRANT_KEY_HEX", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ExperimentRejected("experiment_grant_key_unavailable")
    return bytes.fromhex(value)


def _credential():
    return os.environ.get(_PREFIX + "API_KEY", "")


def _check_path(path: Path, *, directory=False, required=False):
    """Reject aliases and non-private file types before SQLite or grant reads."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        if required:
            raise ExperimentRejected("experiment_storage_missing") from None
        return
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or path.resolve() != path
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or (not directory and info.st_nlink != 1)):
        raise ExperimentRejected("unsafe_experiment_storage")
    if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
        raise ExperimentRejected("unsafe_experiment_storage_permissions")


def _real_root(*, initialize=False):
    root = Path.home().resolve() / _ROOT_NAME
    # A single fixed directory per local operator, outside the project checkout.
    if root.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ExperimentRejected("experiment_storage_inside_checkout")
    if initialize:
        root.mkdir(mode=0o700, exist_ok=True)
    _check_path(root, directory=True, required=True)
    return root


_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    ledger_id TEXT NOT NULL, audience TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiment_requests (
    run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
    grant_sha256 TEXT NOT NULL, sample_sha256 TEXT NOT NULL, model TEXT NOT NULL,
    expires_at REAL NOT NULL, started_at REAL,
    status TEXT NOT NULL CHECK (status IN ('reserved','started','completed','failed','unknown','released'))
);
"""


class _Ledger:
    def __init__(self, root: Path, audience: str, *, initialize=False):
        self.root, self.audience, self.path = root, audience, root / _LEDGER_FILE
        if initialize:
            # Exclusive creation protects the initial file's mode and prevents replacing an old ledger.
            _check_path(root, directory=True, required=True)
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                with self.atomic() as sql:
                    for statement in _SCHEMA.strip().split(";"):
                        if statement.strip():
                            sql.execute(statement)
                    sql.execute("INSERT INTO experiment_identity VALUES(1,?,?)", (uuid4().hex, audience))
        with self.atomic() as sql:
            row = sql.execute("SELECT ledger_id,audience FROM experiment_identity WHERE singleton=1").fetchone()
            if row is None or row["audience"] != audience:
                raise ExperimentRejected("experiment_ledger_identity_mismatch")
            tables = {item[0] for item in sql.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables != {"experiment_identity", "experiment_requests"}:
                raise ExperimentRejected("unexpected_experiment_ledger_schema")
            self.identity = row["ledger_id"]

    @contextmanager
    def atomic(self):
        _check_path(self.root, directory=True, required=True)
        _check_path(self.path, required=True)
        for suffix in ("-journal", "-wal", "-shm"):
            _check_path(self.root / (_LEDGER_FILE + suffix))
        sql = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None)
        sql.row_factory = sqlite3.Row
        try:
            sql.execute("PRAGMA synchronous=FULL")
            sql.execute("BEGIN IMMEDIATE")
            yield sql
            sql.commit()
        finally:
            sql.close()

    def reserve(self, claims, grant_digest, now):
        run = claims["run_id"]
        request_id = f"draft-experiment:{run}:1"
        expires = _parse_time(claims["expires_at"]).timestamp()
        with self.atomic() as sql:
            row = sql.execute("SELECT ledger_id,audience FROM experiment_identity WHERE singleton=1").fetchone()
            if row is None or tuple(row) != (claims["ledger_id"], claims["audience"]):
                raise ExperimentRejected("experiment_ledger_identity_mismatch")
            if expires <= now.timestamp():
                raise ExperimentRejected("experiment_grant_expired")
            if sql.execute("SELECT 1 FROM experiment_requests WHERE run_id=?", (run,)).fetchone():
                raise ExperimentRejected("experiment_budget_consumed")
            sql.execute("INSERT INTO experiment_requests VALUES(?,?,?,?,?,?,NULL,'reserved')",
                        (run, request_id, grant_digest, claims["sample_sha256"], claims["model"], expires))
        return request_id

    def start(self, run, now):
        with self.atomic() as sql:
            changed = sql.execute("UPDATE experiment_requests SET status='started',started_at=? "
                                  "WHERE run_id=? AND status='reserved' AND expires_at>?",
                                  (now.timestamp(), run, now.timestamp())).rowcount
            if changed != 1:
                raise ExperimentRejected("experiment_start_denied")

    def settle(self, run, status):
        if status not in {"completed", "failed", "unknown", "released"}:
            raise ExperimentRejected("invalid_experiment_settlement")
        previous = "reserved" if status == "released" else "started"
        with self.atomic() as sql:
            sql.execute("UPDATE experiment_requests SET status=? WHERE run_id=? AND status=?", (status, run, previous))

    def inspect(self, run, now):
        with self.atomic() as sql:
            sql.execute("UPDATE experiment_requests SET status='unknown' WHERE run_id=? AND status='started' AND started_at<=?",
                        (run, now.timestamp() - _RECOVERY_GRACE))
            sql.execute("UPDATE experiment_requests SET status='released' WHERE run_id=? AND status='reserved' AND expires_at<=?",
                        (run, now.timestamp()))
            row = sql.execute("SELECT run_id,request_id,model,sample_sha256,status FROM experiment_requests WHERE run_id=?",
                              (run,)).fetchone()
            return dict(row) if row else None


@dataclass(frozen=True)
class _Input:
    case: ExperimentCase
    persona_version: str
    messages: list[dict[str, str]]

    @property
    def digest(self):
        return sha256(_canonical(self.messages)).hexdigest()


def _sample(case):
    persona = _catalog_profile(_CHARACTER)
    return _Input(case, persona.version, _sample_messages(case, persona))


_REAL_AUDIENCE = "soulseed-draft-experiment/https/v1"
_MOCK_AUDIENCE = "soulseed-draft-experiment/mock/v1"


def _claims(ledger, provider, run, sample, issued_at, expires_at):
    return {
        "audience": ledger.audience, "ledger_id": ledger.identity, "run_id": run,
        "character_id": _CHARACTER, "case": sample.case.value, "sample_sha256": sample.digest,
        "persona_version": sample.persona_version, "endpoint": provider.endpoint, "model": provider.model,
        "max_requests": 1, "max_output_tokens": _MAX_OUTPUT_TOKENS,
        "request_profile": "deepseek-v41-json-no-thinking-30s-v1",
        "issued_at": _format_time(issued_at), "expires_at": _format_time(expires_at),
    }


def plan_real_experiment(case: ExperimentCase):
    """Return unsigned, content-bound claims. This operation cannot sign or send."""
    provider = _configured_provider()
    ledger = _Ledger(_real_root(initialize=True), _REAL_AUDIENCE, initialize=True)
    issued = _now()
    return _claims(ledger, provider, uuid4().hex, _sample(case), issued, issued + timedelta(minutes=10))


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExperimentRejected("invalid_experiment_grant")
        result[key] = value
    return result


def _read_grant(ledger, run):
    directory = ledger.root / "grants"
    _check_path(directory, directory=True, required=True)
    path = directory / f"{run}.json"
    _check_path(path, required=True)
    try:
        with path.open("rb") as handle:
            data = handle.read(_MAX_GRANT_BYTES + 1)
        if len(data) > _MAX_GRANT_BYTES:
            raise ExperimentRejected("invalid_experiment_grant")
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError) as exc:
        raise ExperimentRejected("invalid_experiment_grant") from exc


def _verify(grant, key, ledger, provider, run, now):
    if (type(grant) is not dict or set(grant) != {"claims", "signature"}
            or type(grant["claims"]) is not dict or not isinstance(grant["signature"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", grant["signature"])):
        raise ExperimentRejected("invalid_experiment_grant")
    claims = grant["claims"]
    try:
        encoded = _canonical(claims)
    except (ValueError, RecursionError):
        raise ExperimentRejected("invalid_experiment_grant") from None
    expected_sig = hmac.digest(key, encoded, "sha256").hex()
    if not hmac.compare_digest(expected_sig, grant["signature"]):
        raise ExperimentRejected("experiment_grant_signature_invalid")
    if claims.get("audience") != ledger.audience:
        raise ExperimentRejected("experiment_grant_audience_mismatch")
    issued, expires = _parse_time(claims.get("issued_at")), _parse_time(claims.get("expires_at"))
    if not issued <= now < expires or not expires <= issued + _MAX_GRANT_AGE:
        raise ExperimentRejected("experiment_grant_expired_or_invalid_window")
    try:
        sample = _sample(ExperimentCase(claims.get("case")))
    except ValueError:
        raise ExperimentRejected("experiment_grant_binding_mismatch") from None
    if encoded != _canonical(_claims(ledger, provider, run, sample, issued, expires)):
        raise ExperimentRejected("experiment_grant_binding_mismatch")
    return sample, sha256(encoded).hexdigest()


async def _response_content(client, provider, credential, body):
    async with asyncio.timeout(_REQUEST_TIMEOUT):
        async with client.stream("POST", provider.endpoint, content=body, headers={
            "Authorization": "Bearer " + credential, "Content-Type": "application/json",
            "Accept-Encoding": "identity",
        }) as response:
            if not response.is_success:
                raise ExperimentRejected("experiment_http_error")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ExperimentRejected("experiment_response_encoding_denied")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise ExperimentRejected("experiment_response_too_large")
                data.extend(chunk)
    try:
        raw = json.loads(data)["choices"][0]["message"]["content"]
        if not isinstance(raw, str):
            raise ValueError
        return raw
    except (ValueError, IndexError, KeyError, TypeError, RecursionError):
        raise ExperimentRejected("experiment_invalid_provider_output") from None


async def _execute(ledger, provider, run, grant, key, *, clock, load_credential, mock_transport=None):
    run = _run_id(run)
    if ((ledger.audience == _MOCK_AUDIENCE and type(mock_transport) is not httpx.MockTransport)
            or (ledger.audience == _REAL_AUDIENCE and mock_transport is not None)
            or ledger.audience not in {_REAL_AUDIENCE, _MOCK_AUDIENCE}):
        raise ExperimentRejected("experiment_transport_not_authorized")
    now = _utc(clock)
    sample, digest = _verify(grant, key, ledger, provider, run, now)
    body = _canonical({"model": provider.model, "messages": sample.messages, "max_tokens": _MAX_OUTPUT_TOKENS,
                       "stream": False, "enable_thinking": False, "response_format": {"type": "json_object"}})
    if len(body) > _MAX_INPUT_BYTES:
        raise ExperimentRejected("experiment_input_too_large")
    request_id = ledger.reserve(grant["claims"], digest, _utc(clock))
    started = False

    async def authorize_request(request):
        nonlocal started
        if request.method != "POST" or str(request.url) != provider.endpoint:
            raise ExperimentRejected("experiment_request_endpoint_mismatch")
        ledger.start(run, _utc(clock))
        started = True

    raw = None
    try:
        credential = load_credential()
        if not isinstance(credential, str) or not re.fullmatch(r"[!-~]{16,1024}", credential):
            raise ExperimentRejected("experiment_credentials_unavailable")
        transport = mock_transport if mock_transport is not None else httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=_REQUEST_TIMEOUT, event_hooks={"request": [authorize_request]}) as client:
            raw = await _response_content(client, provider, credential, body)
        status, reason = "completed", "experiment_request_completed"
    except ExperimentRejected as exc:
        if not started:
            ledger.settle(run, "released")
            raise
        status, reason = "failed", str(exc) if str(exc) in {
            "experiment_http_error", "experiment_response_encoding_denied", "experiment_response_too_large",
            "experiment_invalid_provider_output",
        } else "experiment_response_rejected"
    except asyncio.CancelledError:
        ledger.settle(run, "unknown" if started else "released")
        raise
    except (httpx.TimeoutException, TimeoutError):
        status, reason = ("unknown" if started else "released"), "experiment_request_timeout"
    except Exception:
        if not started:
            ledger.settle(run, "released")
            raise ExperimentRejected("experiment_preflight_failed") from None
        status, reason = "unknown", "experiment_request_outcome_unknown"
    ledger.settle(run, status)
    fields = _review_model_draft(sample.case, raw) if raw is not None else {"reason": reason}
    if raw is not None and (credential in raw or credential in (fields.get("draft_text") or "")):
        fields = {"reason": "experiment_sensitive_output_redacted", "review_status": ReviewStatus.REJECTED}
    return ExperimentReport(
        run_id=run, request_id=request_id, case=sample.case, character_id=_CHARACTER,
        persona_version=sample.persona_version, model_type="authorized_mock" if mock_transport else "authorized_https",
        model_identifier=provider.model, budget_status=status, budget_used=1, call_limit=1,
        sample_sha256=sample.digest, sample_summary=_SAMPLES[sample.case].context,
        reason_code=fields.pop("reason"), **fields,
    )


async def run_real_experiment(run_id: str) -> ExperimentReport:
    """Block until SiliconFlow confirms the model-specific thinking parameter behavior."""
    # The model template supports disabling thinking; the provider's execution
    # of enable_thinking=false for this particular model is not yet verified.
    _run_id(run_id)
    raise ExperimentRejected("experiment_model_thinking_compatibility_unverified")


def inspect_real_experiment(run_id: str):
    """Recover stale metadata and inspect a request without model credentials or transport."""
    run = _run_id(run_id)
    return _Ledger(_real_root(), _REAL_AUDIENCE).inspect(run, _now())
