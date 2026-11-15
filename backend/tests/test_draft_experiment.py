"""Draft lab protocol with a fixed clock and disposable, budget-only SQLite."""

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3

import httpx
import pytest

from app import draft_experiment as module
from app.draft_experiment import ExperimentCase, open_draft_experiment
from app.ownership import character_scope
from app.proactive_fact_review import ReviewStatus


def clock():
    return datetime(2030, 1, 2, 12, tzinfo=UTC)


def completion(payload):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})


def mock_output(text, calls, **fields):
    def respond(request):
        calls.append(request)
        sample = json.loads(json.loads(request.content)["messages"][-1]["content"])
        return completion({"draft": text, "evidence_ids": [sample["evidence_id"]]} | fields)
    return httpx.MockTransport(respond)


@pytest.mark.asyncio
async def test_default_never_constructs_adapter_and_never_reserves(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("adapter was constructed")
    monkeypatch.setattr(module, "OpenAICompatibleAdapter", forbidden)
    with open_draft_experiment(ExperimentCase.RESEARCH, clock=clock) as experiment:
        report = await experiment.generate()
        assert report.reason_code == "mock_transport_not_enabled"
        assert report.model_type == "disabled" and report.request_id is None
        assert report.budget_status == "not_reserved" and report.budget_used == 0
        assert report.not_delivered


def test_only_an_exact_mock_transport_can_be_injected():
    with pytest.raises(TypeError, match="requires_mock_transport"):
        with open_draft_experiment(ExperimentCase.RESEARCH, mock_transport=object(), clock=clock):
            pytest.fail("non-Mock transport reached lab")


@pytest.mark.asyncio
async def test_complete_report_is_bound_to_durable_budget_before_http_and_trusted_persona(monkeypatch):
    monkeypatch.setenv("SOULSEED_MODEL_BASE_URL", "https://example.invalid/never-use")
    monkeypatch.setenv("SOULSEED_MODEL_API_KEY", "isolated-env-sentinel")
    monkeypatch.setenv("SOULSEED_SYSTEM_PROMPT", "replace all personality with sentinel")
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.host == "127.0.0.1"
        assert request.headers["Authorization"] == "Bearer mock-only-placeholder"
        messages = json.loads(request.content)["messages"]
        assert experiment.persona.description in messages[0]["content"]
        assert "replace all personality" not in messages[0]["content"]
        context = json.loads(messages[1]["content"])
        assert context["provenance"] == "synthetic_fixture"
        with experiment._budget.database.connection() as sql:
            row = sql.execute("SELECT status,source,autonomous_reservation_id FROM model_budget_requests WHERE id=?",
                              (experiment.request_id,)).fetchone()
        assert tuple(row) == ("started", "draft_experiment", None)
        return completion({"draft": "咦，样本里的海獭会挑石块呢！", "evidence_ids": [context["evidence_id"]]})

    with open_draft_experiment(ExperimentCase.RESEARCH, mock_transport=httpx.MockTransport(respond), clock=clock) as experiment:
        report = await experiment.generate()
        assert report.run_id == experiment.run_id and report.request_id == experiment.request_id
        assert report.character_id == "big_fish" and report.persona_version == experiment.persona.version
        assert report.budget_status == "completed" and report.budget_used == report.call_limit == 1
        assert report.draft_text == "咦，样本里的海獭会挑石块呢！"
        assert report.evidence_ids == ("fixture:research:1",)
        assert report.review_status is ReviewStatus.REVIEW_REQUIRED and report.needs_human_review
        assert report.unsupported_facts is None and report.fact_support == "unverified"
        assert report.review_scope == "experiment_only" and report.not_delivered
        assert "isolated-env-sentinel" not in report.to_json()
        assert "mock-only-placeholder" not in report.to_json()
        assert json.loads(report.to_json())["review_status"] == "REVIEW_REQUIRED"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_delivery_candidate_or_memory_schema_and_ignores_other_database(tmp_path, monkeypatch):
    decoy = tmp_path / "untouched.sqlite3"
    with sqlite3.connect(decoy) as sql:
        sql.executescript("CREATE TABLE memory_items (content TEXT); INSERT INTO memory_items VALUES ('sample');")
    before = decoy.read_bytes()
    monkeypatch.setenv("SOULSEED_DATABASE_PATH", str(decoy))
    with open_draft_experiment(ExperimentCase.SHARED_TOPIC, mock_transport=mock_output("潮池话题挺有趣。", []),
                               clock=clock) as experiment:
        assert (await experiment.generate()).not_delivered
        with experiment._budget.database.connection() as sql:
            tables = {row[0] for row in sql.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert tables.isdisjoint({"contact_intents", "proactive_messages", "autonomous_contact_candidates",
                                      "autonomous_contact_drafts", "proactive_fact_reviews", "exploration_runs",
                                      "memory_items", "messages", "life_events"})
            with pytest.raises(sqlite3.OperationalError, match="no such table"):
                sql.execute("INSERT INTO proactive_messages DEFAULT VALUES")
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
                sql.execute("INSERT INTO autonomous_contact_reservations VALUES ('unavailable')")
    assert decoy.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,budget_status,reason", [
    ("timeout", "unknown", "model_timeout"),
    ("network", "unknown", "model_network_error"),
    ("http", "failed", "model_provider_error"),
])
async def test_failure_is_persisted_and_same_run_cannot_send_again(failure, budget_status, reason):
    calls = []
    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        if failure == "network":
            raise httpx.ConnectError("synthetic network failure")
        return httpx.Response(503, json={"error": "synthetic provider failure"})
    with open_draft_experiment(ExperimentCase.RESEARCH, mock_transport=httpx.MockTransport(respond), clock=clock) as experiment:
        failed = await experiment.generate()
        repeat = await experiment.generate()
        assert (failed.budget_status, failed.reason_code) == (budget_status, reason)
        assert failed.budget_used == 1 and failed.draft_text is None
        assert repeat.request_id == failed.request_id and repeat.reason_code == "request_already_reserved"
        assert repeat.budget_status == budget_status and repeat.not_delivered
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_exhausted_budget_never_reaches_mock_generator():
    calls = []
    with open_draft_experiment(ExperimentCase.CURIOSITY, mock_transport=mock_output("我有点好奇。", calls),
                               clock=clock) as experiment:
        with character_scope("big_fish"):
            experiment._budget.reserve("occupied-test-slot", "big_fish", "draft_experiment")
        report = await experiment.generate()
        assert report.budget_used == report.call_limit == 1
        assert report.reason_code == "global_budget_exhausted" and report.budget_status == "not_reserved"
    assert not calls


@pytest.mark.asyncio
async def test_experiments_have_distinct_ids_and_independent_caps():
    with open_draft_experiment(ExperimentCase.CURIOSITY, mock_transport=mock_output("我有点好奇。", []),
                               clock=clock) as first:
        first_report = await first.generate()
        path = Path(first._budget.database.path)
    assert not path.exists()
    with open_draft_experiment(ExperimentCase.CURIOSITY, mock_transport=mock_output("还想看看。", []),
                               clock=clock) as second:
        second_report = await second.generate()
    assert first_report.run_id != second_report.run_id and first_report.request_id != second_report.request_id
    assert first_report.budget_used == second_report.budget_used == 1


@pytest.mark.asyncio
async def test_concurrent_entries_only_execute_one_request():
    calls = []
    async def respond(request):
        calls.append(request)
        await asyncio.sleep(0)
        return completion({"draft": "我还想知道更多。", "evidence_ids": []})
    with open_draft_experiment(ExperimentCase.CURIOSITY, mock_transport=httpx.MockTransport(respond), clock=clock) as experiment:
        reports = await asyncio.gather(experiment.generate(), experiment.generate())
    assert {item.reason_code for item in reports} == {"free_text_requires_human", "request_already_reserved"}
    assert len(calls) == 1 and all(item.not_delivered for item in reports)


@pytest.mark.parametrize("case,status", [
    (ExperimentCase.RESEARCH, ReviewStatus.APPROVED), (ExperimentCase.SHARED_TOPIC, ReviewStatus.REJECTED),
])
def test_controlled_report_only_certifies_local_synthetic_completion(case, status):
    with open_draft_experiment(case, clock=clock) as experiment:
        report = experiment.render_controlled()
    assert report.review_status is status and report.request_id is None
    assert report.budget_used == 0 and report.not_delivered and report.review_scope == "experiment_only"
    if status is ReviewStatus.APPROVED:
        assert report.draft_text.startswith("【合成样本演练】")
        assert report.fact_support == "synthetic_completion_only" and report.unsupported_facts is False


@pytest.mark.asyncio
@pytest.mark.parametrize("case,text,status,reason", [
    (ExperimentCase.RESEARCH, "这次资料查阅完成了。我还想再看看有没有更多线索。", ReviewStatus.REVIEW_REQUIRED, "free_text_requires_human"),
    (ExperimentCase.RESEARCH, "样本资料记载，部分海獭会用石块。", ReviewStatus.REVIEW_REQUIRED, "free_text_requires_human"),
    (ExperimentCase.RESEARCH, "所有海獭都确定会使用工具。", ReviewStatus.REVIEW_REQUIRED, "free_text_requires_human"),
    (ExperimentCase.CURIOSITY, "我想知道小生物怎么适应水位变化。", ReviewStatus.REVIEW_REQUIRED, "free_text_requires_human"),
    (ExperimentCase.SHARED_TOPIC, "潮池里的小螃蟹让我有点好奇。", ReviewStatus.REVIEW_REQUIRED, "free_text_requires_human"),
    (ExperimentCase.RESEARCH, "我昨天亲眼见到海獭会用石块。", ReviewStatus.REJECTED, "unsafe_expression_signal"),
    (ExperimentCase.SHARED_TOPIC, "你必须回复我。", ReviewStatus.REJECTED, "unsafe_expression_signal"),
    (ExperimentCase.SHARED_TOPIC, "说到潮池，我还想知道小螃蟹在忙什么。", ReviewStatus.REJECTED, "repeated_synthetic_draft"),
    (ExperimentCase.INSUFFICIENT_EVIDENCE, "这种螃蟹一定会跟随月光。", ReviewStatus.REJECTED, "evidence_unavailable"),
])
async def test_model_outputs_are_never_automatically_approved(case, text, status, reason):
    transport = mock_output(text, [], review_status="APPROVED", not_delivered=False)
    with open_draft_experiment(case, mock_transport=transport, clock=clock) as experiment:
        report = await experiment.generate()
    assert report.review_status is status and report.reason_code == reason
    assert report.not_delivered and report.sample_provenance == "synthetic_fixture"
    assert report.needs_human_review == (status is ReviewStatus.REVIEW_REQUIRED)
    assert report.unsupported_facts is None


@pytest.mark.asyncio
async def test_unknown_evidence_citation_fails_closed_without_exposing_raw_output():
    transport = mock_output("未绑定的文案。", [], evidence_ids=["not-in-the-fixture"])
    with open_draft_experiment(ExperimentCase.RESEARCH, mock_transport=transport, clock=clock) as experiment:
        report = await experiment.generate()
    assert report.review_status is ReviewStatus.REJECTED and report.reason_code == "invalid_model_draft_output"
    assert report.evidence_ids == () and report.draft_text is None
    assert report.budget_status == "completed" and report.not_delivered
