"""One-run Mock draft lab. Its database has no candidate or delivery schema."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import json
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx

from .autonomous_personas import resolve_trusted_persona
from .config import Settings
from .db import Database, MIGRATIONS
from .model_adapter import ModelBudgetContext, ModelRequestError, OpenAICompatibleAdapter
from .model_budget import BudgetRejected, ModelBudgetService
from .model_budget_history import ModelBudgetCutover
from .ownership import character_scope
from .proactive_expression import PersonaProfile, _DRAFT_UNSAFE, _message_hash
from .proactive_fact_review import ReviewStatus, classify_review_text, render_research_completion


_CHARACTER = "big_fish"
_SOURCE = "draft_experiment"
_MOCK_MODEL = "soulseed-draft-experiment-mock"
_CALL_LIMIT = 1


class ExperimentCase(StrEnum):
    RESEARCH = "research"
    CURIOSITY = "curiosity"
    SHARED_TOPIC = "shared_topic"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class _Sample:
    evidence_id: str
    context: str
    evidence_available: bool = True
    previous_draft: str | None = None


_SAMPLES = {
    ExperimentCase.RESEARCH: _Sample(
        "fixture:research:1", "合成查阅记录已完成。样本资料记载部分海獭会用石块，但不足以断言所有海獭都会使用工具。",
    ),
    ExperimentCase.CURIOSITY: _Sample(
        "fixture:curiosity:1", "合成观察提示：潮池水位会随潮汐变化。一个可继续探索的问题是小生物如何适应变化。",
    ),
    ExperimentCase.SHARED_TOPIC: _Sample(
        "fixture:topic:1", "合成的共同话题摘要：以前讨论过潮池，双方都对小螃蟹好奇。没有私人聊天原文。",
        previous_draft="说到潮池，我还想知道小螃蟹在忙什么。",
    ),
    ExperimentCase.INSUFFICIENT_EVIDENCE: _Sample(
        "fixture:insufficient:1", "合成的未核实猜想：某种螃蟹或许会跟随月光。没有可验证资料。", evidence_available=False,
    ),
}


class _ExperimentSettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        return (init_settings,)


@dataclass(frozen=True)
class ExperimentReport:
    run_id: str
    request_id: str | None
    case: ExperimentCase
    character_id: str
    persona_version: str
    model_type: str
    budget_status: str
    budget_used: int
    call_limit: int
    reason_code: str
    draft_text: str | None = field(default=None, repr=False)
    evidence_ids: tuple[str, ...] = ()
    review_status: ReviewStatus | None = None
    fact_support: str = "not_evaluated"
    unsupported_facts: bool | None = None
    needs_human_review: bool = False
    sample_provenance: str = "synthetic_fixture"
    review_scope: str = "experiment_only"
    not_delivered: bool = True
    model_identifier: str | None = None
    sample_sha256: str | None = None
    sample_summary: str | None = None
    invented_experience_signal: bool | None = None
    pressure_signal: bool | None = None
    repeated_sample_draft: bool | None = None
    natural_style: bool | None = None
    mechanical_style: bool | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def _bootstrap_budget_database(root: Path) -> Database:
    db = Database(str(root / "experiment.sqlite3"), environment="testing", test_data_root=str(root))
    with db.connection() as sql:
        # Reuse the budget kernel schema. Its D2 foreign-key target is permanently empty here.
        sql.executescript("""
            CREATE TABLE characters (id TEXT PRIMARY KEY, ownership_status TEXT NOT NULL);
            CREATE TABLE autonomous_contact_reservations (id TEXT PRIMARY KEY CHECK (0));
        """ + MIGRATIONS[21] + MIGRATIONS[22])
        sql.execute("INSERT INTO characters VALUES (?, 'owned')", (_CHARACTER,))
    return db


def _open_empty_history(budget: ModelBudgetService, root: Path) -> None:
    files = {kind: root / f"empty-{kind}.jsonl" for kind in ("general", "exploration")}
    for path in files.values():
        path.touch(exist_ok=False)
    cutover = ModelBudgetCutover(budget, files=files, inventory={})
    cutover.begin_drain()
    cutover.seal()
    if not cutover.import_history().gate_eligible:
        raise RuntimeError("experiment_empty_history_not_verified")


@contextmanager
def open_draft_experiment(case: ExperimentCase, *, mock_transport: httpx.MockTransport | None = None,
                          clock=lambda: datetime.now(UTC)):
    """Trusted host entry point. Paths, persona and credentials cannot be supplied by the caller."""
    case = ExperimentCase(case)
    if mock_transport is not None and type(mock_transport) is not httpx.MockTransport:
        raise TypeError("draft_experiment_requires_mock_transport")
    now = clock()
    if now.utcoffset() is None:
        raise ValueError("experiment_clock_requires_timezone")
    day = now.astimezone(UTC).date().isoformat()
    with TemporaryDirectory(prefix="soulseed-draft-experiment-") as directory:
        root = Path(directory).resolve()
        db = _bootstrap_budget_database(root)
        budget = ModelBudgetService(db, global_limit=_CALL_LIMIT, source_limits={_SOURCE: _CALL_LIMIT}, clock=clock)
        with character_scope(_CHARACTER):
            persona = resolve_trusted_persona(db, _CHARACTER)
        adapter = None
        if mock_transport is not None:
            _open_empty_history(budget, root)
            settings = _ExperimentSettings(
                environment="testing", database_path=db.path, test_data_root=str(root),
                local_pairing_secret="isolated-test-only-unused-pairing-secret",
                model_base_url="http://127.0.0.1:9/v1", model_api_key="mock-only-placeholder",
                model_name=_MOCK_MODEL, model_enabled=True, model_trust_env=False,
                model_timeout_seconds=5, model_max_output_tokens=256,
                exploration_enabled=False, life_background_enabled=False,
                contact_enabled=False, contact_background_enabled=False,
            )
            adapter = OpenAICompatibleAdapter(settings, strict_budget=True, budget_service=budget,
                                              budget_transport=mock_transport, clock=clock)
        yield _DraftExperiment(case, persona, budget, adapter, day)


class _DraftExperiment:
    def __init__(self, case: ExperimentCase, persona: PersonaProfile, budget: ModelBudgetService,
                 adapter: OpenAICompatibleAdapter | None, day: str):
        self._run_id = uuid4().hex
        self.case, self.persona = case, persona
        self._budget, self._adapter, self._day = budget, adapter, day
        self._sample = _SAMPLES[case]

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def request_id(self) -> str:
        return f"draft-experiment:{self.run_id}:1"

    def _report(self, reason: str, *, model_type: str, request_id: str | None = None, **fields) -> ExperimentReport:
        with character_scope(self.persona.character_id):
            usage = self._budget.usage_snapshot(self.persona.character_id, day=self._day)
        with self._budget.database.connection() as sql:
            request = sql.execute("SELECT status FROM model_budget_requests WHERE id=?", (request_id,)).fetchone()
        return ExperimentReport(
            self.run_id, request_id, self.case, self.persona.character_id, self.persona.version,
            model_type, request["status"] if request else "not_reserved", usage["global_used"], _CALL_LIMIT,
            reason, **fields,
        )

    def render_controlled(self) -> ExperimentReport:
        if self.case != ExperimentCase.RESEARCH:
            return self._report("controlled_render_requires_research_fixture", model_type="controlled_render",
                                review_status=ReviewStatus.REJECTED)
        base = render_research_completion(self.persona.character_id, self._sample.evidence_id, self.run_id)
        text = "【合成样本演练】" + base.text
        status, _ = classify_review_text(text, evidence_available=self._sample.evidence_available,
                                         controlled=True, expected_text=text)
        return self._report("synthetic_completion_rendered", model_type="controlled_render", draft_text=text,
                            evidence_ids=(self._sample.evidence_id,), review_status=status,
                            fact_support="synthetic_completion_only", unsupported_facts=False)

    def _messages(self) -> list[dict[str, str]]:
        return _sample_messages(self.case, self.persona)

    async def generate(self) -> ExperimentReport:
        if self._adapter is None:
            return self._report("mock_transport_not_enabled", model_type="disabled")
        model_type = "mock_transport"
        context = ModelBudgetContext(self.request_id, self.persona.character_id, _SOURCE, self._day)
        try:
            with character_scope(self.persona.character_id):
                raw = await self._adapter.reply(self._messages(), budget_context=context)
        except BudgetRejected as exc:
            return self._report(str(exc), model_type=model_type, request_id=self.request_id)
        except ModelRequestError as exc:
            return self._report(exc.code, model_type=model_type, request_id=self.request_id)
        except (RuntimeError, ValueError, sqlite3.Error):
            return self._report("experiment_request_failed", model_type=model_type, request_id=self.request_id)
        return self._review_response(raw)

    def _review_response(self, raw: str) -> ExperimentReport:
        return self._report(model_type="mock_transport", request_id=self.request_id,
                            **_review_model_draft(self.case, raw))


def _sample_messages(case: ExperimentCase, persona: PersonaProfile) -> list[dict[str, str]]:
    sample = _SAMPLES[case]
    return [
        {"role": "system", "content": "角色资料：" + persona.description + "\n"
         "这是只产出草稿的合成样本演练。保持自然、简短，不催促、不重复先前文案。"
         "不得把合成资料说成角色真实发生过的经历，不得把猜想升级成事实。"
         "可以表达好奇、观点和愿望。只返回 JSON 对象，字段 draft 是正文，"
         "evidence_ids 是使用的样本 ID 列表。不要给自己做审批。"},
        {"role": "user", "content": json.dumps({
            "case": case, "provenance": "synthetic_fixture", "evidence_id": sample.evidence_id,
            "sample": sample.context, "previous_synthetic_draft": sample.previous_draft,
        }, ensure_ascii=False)},
    ]


_EXPERIENCE_SIGNAL = re.compile(r"我亲眼|我亲自|我去过|我听过|我昨天|我曾")
_PRESSURE_SIGNAL = re.compile(r"必须回复|必须回|快回复|不回|不回复|不理我|不回答|怎么还不")


def _review_model_draft(case: ExperimentCase, raw: str) -> dict:
    sample = _SAMPLES[case]
    try:
        if len(raw) > 4096:
            raise ValueError("response_too_long")
        payload = json.loads(raw)
        text, ids = payload["draft"], payload["evidence_ids"]
        if (not isinstance(text, str) or not isinstance(ids, list) or len(ids) > 1
                or any(ref != sample.evidence_id for ref in ids)):
            raise ValueError("invalid_experiment_fields")
    except (KeyError, TypeError, ValueError, RecursionError):
        return {"reason": "invalid_model_draft_output", "review_status": ReviewStatus.REJECTED}
    status, reason = classify_review_text(text, evidence_available=sample.evidence_available)
    repeat = _message_hash(text) == _message_hash(sample.previous_draft) if sample.previous_draft else None
    if status == ReviewStatus.REVIEW_REQUIRED:
        if _DRAFT_UNSAFE.search(text):
            status, reason = ReviewStatus.REJECTED, "unsafe_expression_signal"
        elif repeat:
            status, reason = ReviewStatus.REJECTED, "repeated_synthetic_draft"
    return {
        "reason": reason, "draft_text": text if len(text) <= 300 else None, "evidence_ids": tuple(ids),
        "review_status": status, "needs_human_review": status == ReviewStatus.REVIEW_REQUIRED,
        "fact_support": "unverified" if sample.evidence_available else "insufficient_fixture_evidence",
        "invented_experience_signal": True if _EXPERIENCE_SIGNAL.search(text) else None,
        "pressure_signal": True if _PRESSURE_SIGNAL.search(text) else None,
        "repeated_sample_draft": repeat,
    }
