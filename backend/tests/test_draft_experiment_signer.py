from datetime import UTC, datetime, timedelta
import json
import uuid

import pytest

from app import authorized_draft_experiment as lab
from app import draft_experiment_signer as signer
from app.draft_experiment import ExperimentCase
from app.proactive_fact_review import ReviewStatus
import httpx


KEY = bytes(range(32))
ENDPOINT = "https://api.siliconflow.cn/v1/chat/completions"


@pytest.fixture
def signing_fixture(tmp_path, monkeypatch):
    root = tmp_path / ".soulseed-draft-experiments"
    root.mkdir()
    ledger = lab._Ledger(root, lab._REAL_AUDIENCE, initialize=True)
    provider = lab._Provider(ENDPOINT, "offline-test/model")
    now = datetime.now(UTC).replace(microsecond=0)
    run = uuid.uuid4().hex
    sample = lab._sample(ExperimentCase.RESEARCH)
    claims = lab._claims(ledger, provider, run, sample, now, now + timedelta(minutes=10))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(claims, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(signer.lab, "_real_root", lambda: root)
    monkeypatch.setattr(signer.lab, "_configured_provider", lambda: provider)
    return root, ledger, provider, now, run, claims, plan


def test_signer_installs_exactly_one_offline_bound_grant(signing_fixture):
    root, ledger, provider, now, run, claims, plan = signing_fixture
    result = signer.sign_plan_file(plan, confirmed_run_id=run, key=KEY, now=now)
    assert result == run
    installed = json.loads((root / "grants" / f"{run}.json").read_text(encoding="utf-8"))
    assert installed == {"claims": claims, "signature": lab.hmac.digest(KEY, lab._canonical(claims), "sha256").hex()}
    with pytest.raises(lab.ExperimentRejected, match="already_exists"):
        signer.sign_plan_file(plan, confirmed_run_id=run, key=KEY, now=now)


@pytest.mark.parametrize("edit", [
    lambda claims: claims | {"model": "attacker/changed"},
    lambda claims: claims | {"sample_sha256": "0" * 64},
    lambda claims: claims | {"max_requests": 2},
    lambda claims: claims | {"character_id": "other"},
])
def test_signer_recomputes_plan_and_rejects_modified_claims(signing_fixture, edit):
    _, _, _, now, run, claims, plan = signing_fixture
    plan.write_text(json.dumps(edit(claims)), encoding="utf-8")
    with pytest.raises(lab.ExperimentRejected, match="binding_mismatch"):
        signer.sign_plan_file(plan, confirmed_run_id=run, key=KEY, now=now)


def test_signer_rejects_wrong_confirmation_expiry_and_consumed_ledger(signing_fixture):
    root, ledger, _, now, run, claims, plan = signing_fixture
    with pytest.raises(lab.ExperimentRejected, match="confirmation_mismatch"):
        signer.sign_plan_file(plan, confirmed_run_id="0" * 32, key=KEY, now=now)
    with pytest.raises(lab.ExperimentRejected, match="plan_binding_mismatch"):
        signer.sign_plan_file(plan, confirmed_run_id=run, key=KEY, now=now + timedelta(minutes=11))
    ledger.reserve(claims, "prior-grant", now)
    with pytest.raises(lab.ExperimentRejected, match="budget_consumed"):
        signer.sign_plan_file(plan, confirmed_run_id=run, key=KEY, now=now)
    assert not (root / "grants" / f"{run}.json").exists()


def test_signer_rejects_replaced_ledger_and_nonprivate_plan_alias(signing_fixture, tmp_path):
    root, _, _, now, run, _, plan = signing_fixture
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    lab._Ledger(replacement, lab._REAL_AUDIENCE, initialize=True)
    old_root = root
    signer.lab._real_root = lambda: replacement
    with pytest.raises(lab.ExperimentRejected, match="plan_binding_mismatch|binding_mismatch"):
        signer.sign_plan_file(plan, confirmed_run_id=run, key=KEY, now=now)
    signer.lab._real_root = lambda: old_root


def test_environment_signer_loads_key_only_after_confirmation_and_never_reads_api_key(signing_fixture, monkeypatch):
    root, _, provider, now, run, _, plan = signing_fixture
    monkeypatch.setenv("SOULSEED_DRAFT_EXPERIMENT_GRANT_KEY_HEX", KEY.hex())
    monkeypatch.delenv("SOULSEED_DRAFT_EXPERIMENT_API_KEY", raising=False)
    assert signer.sign_real_plan_from_environment(plan, confirmed_run_id=run) == run
    assert (root / "grants" / f"{run}.json").is_file()


@pytest.mark.asyncio
async def test_full_offline_preview_sign_verify_mock_http_and_restart(signing_fixture, monkeypatch, tmp_path):
    root, ledger, provider, now, run, claims, plan = signing_fixture
    signer.sign_plan_file(plan, confirmed_run_id=run, key=KEY, now=now)
    grant = lab._read_grant(ledger, run)
    sample, _ = lab._verify(grant, KEY, ledger, provider, run, now)
    assert sample.digest == claims["sample_sha256"]

    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "draft": "样本里记下了资料查阅完成，我还想多了解一点。",
            "evidence_ids": ["fixture:research:1"],
        })}}]})

    monkeypatch.setattr(lab.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(respond))
    report = await lab._execute(ledger, provider, run, grant, KEY, clock=lambda: now,
                                load_credential=lambda: "synthetic-offline-credential")
    assert len(calls) == 1 and report.budget_status == "completed"
    assert report.review_status is ReviewStatus.REVIEW_REQUIRED and report.not_delivered
    restarted = lab._Ledger(root, lab._REAL_AUDIENCE)
    assert restarted.inspect(run, now)["status"] == "completed"
    with pytest.raises(lab.ExperimentRejected, match="budget_consumed"):
        await lab._execute(restarted, provider, run, grant, KEY, clock=lambda: now,
                           load_credential=lambda: pytest.fail("replay read a credential"))
    assert len(calls) == 1


def test_signed_authorization_is_invalid_for_replacement_ledger(signing_fixture, tmp_path, monkeypatch):
    root, _, provider, now, run, _, plan = signing_fixture
    signer.sign_plan_file(plan, confirmed_run_id=run, key=KEY, now=now)
    old = lab._Ledger(root, lab._REAL_AUDIENCE)
    grant = lab._read_grant(old, run)
    replacement_root = tmp_path / "replacement-ledger"
    replacement_root.mkdir()
    replacement = lab._Ledger(replacement_root, lab._REAL_AUDIENCE, initialize=True)
    with pytest.raises(lab.ExperimentRejected, match="signature_invalid|binding_mismatch"):
        lab._verify(grant, KEY, replacement, provider, run, now)
