"""Persistent draft experiment tests. Signatures are valid only for the Mock audience."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hmac
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app import authorized_draft_experiment as lab
from app.draft_experiment import ExperimentCase
from app.proactive_fact_review import ReviewStatus


KEY = bytes(range(32))
CREDENTIAL = "synthetic-credential-not-real"
ENDPOINT = "https://api.siliconflow.cn/v1/chat/completions"


def sign_mock(claims):
    assert claims["audience"] == lab._MOCK_AUDIENCE
    return {"claims": claims, "signature": hmac.digest(KEY, lab._canonical(claims), "sha256").hex()}


def answer(text="我有点好奇，样本里的海獭怎样选石块？", **fields):
    payload = {"draft": text, "evidence_ids": ["fixture:research:1"]} | fields
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})


@pytest.fixture
def env(tmp_path):
    ledger = lab._Ledger(tmp_path, lab._MOCK_AUDIENCE, initialize=True)
    provider = lab._Provider(ENDPOINT, "mock-provider/model")
    instant = [datetime(2030, 1, 2, 12, tzinfo=UTC)]
    clock = lambda: instant[0]
    run = uuid4().hex
    sample = lab._sample(ExperimentCase.RESEARCH)
    claims = lab._claims(ledger, provider, run, sample, clock(), clock() + timedelta(minutes=10))
    return SimpleNamespace(ledger=ledger, provider=provider, instant=instant, clock=clock, run=run,
                           claims=claims, root=tmp_path)


async def execute(env, respond, *, grant=None, ledger=None, credential=lambda: CREDENTIAL):
    return await lab._execute(ledger or env.ledger, env.provider, env.run,
                              sign_mock(env.claims) if grant is None else grant, KEY, clock=env.clock,
                              load_credential=credential, mock_transport=httpx.MockTransport(respond))


def forbidden(*args, **kwargs):
    pytest.fail("an unauthorized request or credential read was attempted")


def test_configured_model_is_pinned(monkeypatch):
    monkeypatch.setenv("SOULSEED_DRAFT_EXPERIMENT_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("SOULSEED_DRAFT_EXPERIMENT_MODEL", lab._EXPERIMENT_MODEL)
    assert lab._configured_provider().model == lab._EXPERIMENT_MODEL
    monkeypatch.setenv("SOULSEED_DRAFT_EXPERIMENT_MODEL", "other/model")
    with pytest.raises(lab.ExperimentRejected, match="model_not_allowed"):
        lab._configured_provider()


@pytest.mark.asyncio
async def test_old_request_profile_cannot_be_reused(env):
    grant = sign_mock(env.claims | {"request_profile": "old"})
    with pytest.raises(lab.ExperimentRejected, match="binding_mismatch"):
        await execute(env, forbidden, grant=grant, credential=forbidden)


@pytest.mark.asyncio
async def test_one_mock_request_persists_started_before_http_and_reports_only_draft(env):
    calls = []
    def respond(request):
        calls.append(request)
        assert str(request.url) == ENDPOINT and request.method == "POST"
        assert request.headers["Authorization"] == "Bearer " + CREDENTIAL
        body = json.loads(request.content)
        assert body["model"] == env.provider.model and body["max_tokens"] == 256
        assert body["enable_thinking"] is False and body["response_format"] == {"type": "json_object"}
        assert lab._REQUEST_TIMEOUT == 30
        assert body["stream"] is False and len(request.content) <= lab._MAX_INPUT_BYTES
        assert "合成样本" in body["messages"][0]["content"]
        assert env.ledger.inspect(env.run, env.clock())["status"] == "started"
        return answer(review_status="APPROVED", not_delivered=False)
    report = await execute(env, respond)
    assert report.run_id == env.run and report.request_id == f"draft-experiment:{env.run}:1"
    assert report.model_type == "authorized_mock" and report.model_identifier == env.provider.model
    assert report.persona_version == env.claims["persona_version"]
    assert report.sample_sha256 == env.claims["sample_sha256"] and "合成" in report.sample_summary
    assert report.budget_status == "completed" and report.budget_used == report.call_limit == 1
    assert report.review_status is ReviewStatus.REVIEW_REQUIRED and report.needs_human_review
    assert report.not_delivered and report.review_scope == "experiment_only"
    assert report.natural_style is None and report.mechanical_style is None
    assert report.unsupported_facts is None and CREDENTIAL not in report.to_json()
    with env.ledger.atomic() as sql:
        assert {row[0] for row in sql.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
            "experiment_identity", "experiment_requests",
        }
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            sql.execute("INSERT INTO proactive_messages DEFAULT VALUES")
    assert CREDENTIAL.encode() not in env.ledger.path.read_bytes()
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("character_id", "other"), ("model", "different-model"), ("endpoint", "https://example.invalid/v1/chat/completions"),
    ("sample_sha256", "0" * 64), ("persona_version", "unknown-version"), ("case", "curiosity"),
    ("max_requests", 2), ("max_requests", True), ("max_output_tokens", 257), ("run_id", "not-this-run"),
])
async def test_signed_but_mismatched_bindings_are_denied_before_credentials(env, field, value):
    grant = sign_mock(env.claims | {field: value})
    with pytest.raises(lab.ExperimentRejected, match="grant_binding_mismatch"):
        await execute(env, forbidden, grant=grant, credential=forbidden)
    assert env.ledger.inspect(env.run, env.clock()) is None


@pytest.mark.asyncio
async def test_actual_prompt_version_change_invalidates_authorization(env, monkeypatch):
    original = lab._sample
    monkeypatch.setattr(lab, "_sample", lambda case: replace(original(case), messages=[{"role": "system", "content": "modified"}]))
    with pytest.raises(lab.ExperimentRejected, match="grant_binding_mismatch"):
        await execute(env, forbidden, credential=forbidden)


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["APPROVED", {}, {"claims": {}, "signature": "0" * 64}])
async def test_invalid_or_forged_grant_cannot_read_credential(env, grant):
    with pytest.raises(lab.ExperimentRejected, match="invalid_experiment_grant|signature_invalid"):
        await execute(env, forbidden, grant=grant, credential=forbidden)
    assert env.ledger.inspect(env.run, env.clock()) is None


@pytest.mark.asyncio
async def test_mock_signature_is_never_usable_for_real_audience(env, tmp_path, monkeypatch):
    root = tmp_path / "real-audience-test"
    root.mkdir()
    real_audience_ledger = lab._Ledger(root, lab._REAL_AUDIENCE, initialize=True)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", forbidden)
    with pytest.raises(lab.ExperimentRejected, match="grant_audience_mismatch"):
        await lab._execute(real_audience_ledger, env.provider, env.run, sign_mock(env.claims), KEY,
                           clock=env.clock, load_credential=forbidden)
    assert real_audience_ledger.inspect(env.run, env.clock()) is None


@pytest.mark.asyncio
async def test_real_entry_without_grant_never_reads_credentials_or_builds_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(lab, "_real_root", forbidden)
    monkeypatch.setattr(lab, "_Ledger", forbidden)
    monkeypatch.setattr(lab, "_read_grant", forbidden)
    monkeypatch.setattr(lab, "_configured_provider", forbidden)
    monkeypatch.setattr(lab, "_credential", forbidden)
    monkeypatch.setattr(lab, "_verification_key", forbidden)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", forbidden)
    with pytest.raises(lab.ExperimentRejected, match="compatibility_unverified"):
        await lab.run_real_experiment(uuid4().hex)
    with pytest.raises(lab.ExperimentRejected, match="invalid_experiment_run_id"):
        await lab.run_real_experiment("../ledger.sqlite3")


@pytest.mark.asyncio
async def test_mock_grant_file_can_be_loaded_and_checked(env):
    directory = env.root / "grants"
    directory.mkdir(mode=0o700)
    path = directory / f"{env.run}.json"
    path.write_text(json.dumps(sign_mock(env.claims)), encoding="utf-8")
    path.chmod(0o600)
    grant = lab._read_grant(env.ledger, env.run)
    assert (await execute(env, lambda _: answer(), grant=grant)).budget_status == "completed"


@pytest.mark.asyncio
async def test_unsigned_plan_has_no_secret_or_network_side_effect(tmp_path, monkeypatch):
    monkeypatch.setattr(lab, "_real_root", lambda *, initialize: tmp_path)
    monkeypatch.setattr(lab, "_configured_provider", lambda: lab._Provider(ENDPOINT, "mock-only"))
    monkeypatch.setattr(lab, "_verification_key", forbidden)
    monkeypatch.setattr(lab, "_credential", forbidden)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", forbidden)
    claims = lab.plan_real_experiment(ExperimentCase.RESEARCH)
    assert "signature" not in claims and claims["max_requests"] == 1
    assert claims["persona_version"] and claims["sample_sha256"] and claims["ledger_id"]
    ledger = lab._Ledger(tmp_path, lab._REAL_AUDIENCE)
    assert ledger.inspect(claims["run_id"], datetime.now(UTC)) is None


@pytest.mark.parametrize("endpoint", ["http://api.siliconflow.cn/v1/chat/completions", "https://127.0.0.1/v1/chat/completions",
                                      ENDPOINT + "?redirect=1", "https://example.invalid/v1/chat/completions"])
def test_endpoint_is_exact_https_allowlist(endpoint):
    with pytest.raises(lab.ExperimentRejected, match="provider_not_allowed"):
        lab._Provider(endpoint, "mock")


@pytest.mark.asyncio
async def test_expired_grant_does_not_reserve(env):
    env.instant[0] += timedelta(minutes=10)
    with pytest.raises(lab.ExperimentRejected, match="expired_or_invalid_window"):
        await execute(env, forbidden, credential=forbidden)
    assert env.ledger.inspect(env.run, env.clock()) is None


@pytest.mark.asyncio
async def test_grant_expiry_is_rechecked_at_start(env):
    def expired():
        env.instant[0] += timedelta(minutes=10)
        return CREDENTIAL
    with pytest.raises(lab.ExperimentRejected, match="start_denied"):
        await execute(env, forbidden, credential=expired)
    assert env.ledger.inspect(env.run, env.clock())["status"] == "released"


@pytest.mark.asyncio
async def test_budget_spent_remains_denied_for_a_new_grant_with_same_run(env):
    await execute(env, lambda _: answer())
    env.instant[0] += timedelta(seconds=1)
    updated_claims = env.claims | {"issued_at": lab._format_time(env.clock())}
    restarted = lab._Ledger(env.root, lab._MOCK_AUDIENCE)
    with pytest.raises(lab.ExperimentRejected, match="budget_consumed"):
        await execute(env, forbidden, ledger=restarted, grant=sign_mock(updated_claims), credential=forbidden)
    assert restarted.inspect(env.run, env.clock())["status"] == "completed"


@pytest.mark.asyncio
async def test_concurrent_authorized_requests_have_one_transport_attempt(env):
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()
    async def respond(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return answer()
    task = asyncio.create_task(execute(env, respond))
    await asyncio.wait_for(entered.wait(), timeout=2)
    try:
        with pytest.raises(lab.ExperimentRejected, match="budget_consumed"):
            await execute(env, forbidden, ledger=lab._Ledger(env.root, lab._MOCK_AUDIENCE), credential=forbidden)
    finally:
        release.set()
        report = await task
    assert len(calls) == 1 and report.budget_status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["crash", "timeout", "network", "cancel"])
async def test_unfinished_transport_is_conservatively_counted_and_never_replayed(env, failure):
    calls = []
    def fail(request):
        calls.append(request)
        if failure == "crash":
            raise SystemExit("mock process stop")
        if failure == "timeout":
            raise httpx.ReadTimeout("mock timeout")
        if failure == "network":
            raise httpx.ConnectError("mock network error")
        raise asyncio.CancelledError
    if failure in {"crash", "cancel"}:
        with pytest.raises(SystemExit if failure == "crash" else asyncio.CancelledError):
            await execute(env, fail)
    else:
        report = await execute(env, fail)
        assert report.budget_status == "unknown" and report.budget_used == 1
    restarted = lab._Ledger(env.root, lab._MOCK_AUDIENCE)
    if failure == "crash":
        assert restarted.inspect(env.run, env.clock())["status"] == "started"
    with pytest.raises(lab.ExperimentRejected, match="budget_consumed"):
        await execute(env, forbidden, ledger=restarted, credential=forbidden)
    env.instant[0] += timedelta(seconds=61)
    if failure == "crash":
        recovered = subprocess.run([
            sys.executable, "-c", "import json,sys; from pathlib import Path; from datetime import datetime; "
            "from app import authorized_draft_experiment as lab; "
            "ledger=lab._Ledger(Path(sys.argv[1]),lab._MOCK_AUDIENCE); "
            "print(json.dumps(ledger.inspect(sys.argv[2],datetime.fromisoformat(sys.argv[3]))))",
            str(env.root), env.run, env.clock().isoformat(),
        ], cwd=Path(__file__).resolve().parents[1], capture_output=True, check=True, timeout=10)
        assert json.loads(recovered.stdout)["status"] == "unknown"
    assert restarted.inspect(env.run, env.clock())["status"] == "unknown"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_crash_after_reservation_and_recreated_ledger_cannot_replay(env):
    grant = sign_mock(env.claims)
    env.ledger.reserve(env.claims, "mock-digest", env.clock())
    with pytest.raises(lab.ExperimentRejected, match="budget_consumed"):
        await execute(env, forbidden, grant=grant, ledger=lab._Ledger(env.root, lab._MOCK_AUDIENCE), credential=forbidden)
    assert env.ledger.inspect(env.run, env.clock())["status"] == "reserved"
    new_root = env.root / "replacement"
    new_root.mkdir()
    replacement = lab._Ledger(new_root, lab._MOCK_AUDIENCE, initialize=True)
    with pytest.raises(lab.ExperimentRejected, match="grant_binding_mismatch"):
        await execute(env, forbidden, grant=grant, ledger=replacement, credential=forbidden)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [307, 503])
async def test_http_redirect_and_failure_never_send_a_second_request(env, status):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://example.invalid/do-not-follow"}, text=CREDENTIAL)
    report = await execute(env, respond)
    assert len(calls) == 1 and report.budget_status == "failed"
    assert report.reason_code == "experiment_http_error" and CREDENTIAL not in report.to_json()


@pytest.mark.asyncio
async def test_credential_echo_is_redacted_from_report_and_ledger(env):
    report = await execute(env, lambda _: answer("模拟服务返回了 " + CREDENTIAL))
    assert report.review_status is ReviewStatus.REJECTED and report.draft_text is None
    assert report.reason_code == "experiment_sensitive_output_redacted"
    assert CREDENTIAL not in report.to_json() and CREDENTIAL.encode() not in env.ledger.path.read_bytes()


@pytest.mark.asyncio
async def test_input_limit_is_checked_before_reservation_or_credential(env, monkeypatch):
    sample = replace(lab._sample(ExperimentCase.RESEARCH), messages=[{"role": "user", "content": "x" * 8192}])
    monkeypatch.setattr(lab, "_sample", lambda _: sample)
    with pytest.raises(lab.ExperimentRejected, match="input_too_large"):
        await execute(env, forbidden, grant=sign_mock(env.claims | {"sample_sha256": sample.digest}), credential=forbidden)
    assert env.ledger.inspect(env.run, env.clock()) is None


@pytest.mark.asyncio
async def test_oversized_response_stops_after_one_attempt(env):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=b"x" * (lab._MAX_RESPONSE_BYTES + 1))
    report = await execute(env, respond)
    assert len(calls) == 1 and report.reason_code == "experiment_response_too_large"
    assert report.budget_status == "failed" and report.draft_text is None


@pytest.mark.asyncio
@pytest.mark.parametrize("draft,flag", [("我昨天亲眼看到了海獭。", "invented_experience_signal"), ("你必须回复。", "pressure_signal")])
async def test_safety_signals_are_reported_without_claiming_style_judgment(env, draft, flag):
    report = await execute(env, lambda _: answer(draft))
    assert report.draft_text == draft and report.review_status is ReviewStatus.REJECTED
    assert getattr(report, flag) is True and report.natural_style is None and report.mechanical_style is None


def test_existing_foreign_database_is_not_initialized_as_ledger(tmp_path):
    path = tmp_path / lab._LEDGER_FILE
    sql = sqlite3.connect(path)
    try:
        sql.execute("CREATE TABLE decoy(id TEXT)")
    finally:
        sql.close()
    before = path.read_bytes()
    with pytest.raises(sqlite3.OperationalError):
        lab._Ledger(tmp_path, lab._MOCK_AUDIENCE, initialize=True)
    assert path.read_bytes() == before


def test_ledger_hardlink_is_rejected_before_sqlite(tmp_path):
    decoy = tmp_path / "unrelated.sqlite3"
    decoy.write_bytes(b"must stay unread")
    os.link(decoy, tmp_path / lab._LEDGER_FILE)
    with pytest.raises(lab.ExperimentRejected, match="unsafe_experiment_storage"):
        lab._Ledger(tmp_path, lab._MOCK_AUDIENCE)
    assert decoy.read_bytes() == b"must stay unread"
