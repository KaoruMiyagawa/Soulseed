import asyncio
import ipaddress
import logging
from hmac import compare_digest
from pathlib import Path
from contextlib import asynccontextmanager
from copy import copy

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfoNotFoundError

from .chat_service import ChatService, reply_execution
from .chat_reply_worker import ChatReplyWorker, ConversationReplyBusy, DelayedMockChatModel, ReplyQueue
from .chat_reply_review import ChatReplyReview
from .context_window import ContextBudgetExceeded, ContextWindowLimits, ModelContextWindow
from .config import get_settings
from .db import Database
from .desktop_runtime import ModelInput, RuntimeLimitsInput, runtime as desktop
from .desktop_contact import DesktopContact
from .autonomous_social import AutonomousContact
from .model_adapter import (ModelConfigurationError, ModelRequestError, OpenAICompatibleAdapter,
                            ModelDailyQuotaExhausted, ModelQuotaUnavailable, ModelBudgetContext)
from .model_budget import BudgetRejected, ModelBudgetService
from .model_budget_history import ModelBudgetCutover
from .memory import ClarificationApplicationService, ClarificationRepository, CorrectionFlowService, DeterministicMemoryExtractor, MemoryContextBuilder, MemoryCorrectionService, MemoryProposalRepository, MemoryProposalService, MemoryRepository, MemoryRetriever, MemoryService
from .persona import resolve_system_prompt
from .proposal_api import create_proposal_router
from .repositories import ConversationRepository, MessageRepository
from .message_delivery import MessageDeliveryRepository
from .life import LifeRepository
from .life_engine import LifeEngine
from .life_scheduler import LifeScheduler
from .life_instance import LifeInstance
from .exploration_agent import ExplorationAgent
from .exploration_repository import ExplorationRepository
from .life_share import LifeShareService
from .contact import AdapterContactGenerator, ContactService
from .contact_scheduler import ContactScheduler
from .autonomous_intent_shadow import ShadowModeScheduler
from .build_info import BUILD_ID, STARTUP_ID
from .local_auth import LocalAuth, COOKIE, ALLOWED_ORIGINS
from .ownership import character_scope, current_character


class CreateConversationRequest(BaseModel):
    title: str = Field(default="新会话", min_length=1, max_length=120)


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


class IndependentMessageRequest(BaseModel):
    client_message_id: str = Field(min_length=8, max_length=120)
    client_stream_id: str = Field(min_length=8, max_length=120)
    client_sequence: int = Field(ge=1, le=2**63 - 1)
    content: str = Field(min_length=1, max_length=20000)


class ChatReplyReviewRequest(BaseModel):
    snapshot_token: str = Field(min_length=64, max_length=64)
    decision: Literal["close_without_reply", "confirm_saved_reply"]
    evidence_reference: str = Field(min_length=8, max_length=500)
    provider_checked: bool = False


class LifeEventRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    event_type: str = Field(default="activity", min_length=1, max_length=80)
    status: Literal["completed", "planned", "imagined", "wish"] = "completed"
    occurred_at: datetime | None = None
    planned_start_at: datetime | None = None
    planned_end_at: datetime | None = None
    location: str | None = Field(default=None, max_length=200)
    feeling: str | None = Field(default=None, max_length=200)
    source: str = Field(default="character", max_length=80)
    generation_method: str = Field(default="manual", max_length=80)
    idempotency_key: str | None = Field(default=None, max_length=200)


class ActivityTransitionRequest(BaseModel):
    target: Literal["active", "completed", "cancelled"]
    reason: str = Field(default="manual", max_length=200)
    feeling: str | None = Field(default=None, max_length=200)


class TimezoneRequest(BaseModel):
    timezone_id: str = Field(min_length=1, max_length=100)


class ShareTransitionRequest(BaseModel):
    target: Literal["delivered", "dismissed"]


class ContactPreferencesRequest(BaseModel):
    allowed: bool | None = None
    busy_until: datetime | None = None
    pause_until: datetime | None = None
    quiet_start_minute: int | None = Field(default=None, ge=0, le=1439)
    quiet_end_minute: int | None = Field(default=None, ge=0, le=1439)


class ContactIntentRequest(BaseModel):
    source: Literal["life_share", "smalltalk", "conversation_followup", "reminder"]
    source_id: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=200)
    topic: str | None = Field(default=None, max_length=120)
    importance: Literal["normal", "important"] = "normal"


class DeliverContactRequest(BaseModel):
    conversation_id: str


class PairRequest(BaseModel):
    secret: str = Field(min_length=1, max_length=512, repr=False)
    character_id: str = Field(default="big_fish", min_length=1, max_length=120)


class StartExplorationRequest(BaseModel):
    topic: str | None = Field(default=None, min_length=3, max_length=240)
    idempotency_key: str = Field(min_length=8, max_length=120)
    retry_run_id: str | None = Field(default=None, min_length=1, max_length=120)


class ExplorationAutonomyRequest(BaseModel):
    enabled: bool | None = None
    paused_until: datetime | None = None
    cooldown_hours: int | None = Field(default=None, ge=24, le=720)


settings = desktop.settings if desktop else get_settings()
allowed_origins = set(ALLOWED_ORIGINS)
if desktop:
    allowed_origins.clear()
if settings.environment == "testing" and settings.test_frontend_origin:
    allowed_origins.add(settings.test_frontend_origin.rstrip("/"))
if settings.environment == "development" and settings.development_frontend_origin:
    allowed_origins = {settings.development_frontend_origin.rstrip("/")}
database_path_label = (
    "<test-root>/" + Path(settings.database_path).relative_to(Path(settings.test_data_root).resolve()).as_posix()
    if settings.environment == "testing"
    else "<database>/" + Path(settings.database_path).name
)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("soulseed.startup")
logger.info("Soulseed environment=%s database=%s", settings.environment, database_path_label)
logger.info("Soulseed runtime build_id=%s startup_id=%s", BUILD_ID, STARTUP_ID)
database = desktop.database if desktop else Database(
    settings.database_path,
    environment=settings.environment,
    test_data_root=settings.test_data_root,
    migration_backup_path=settings.migration_backup_path,
    migration_confirmation=settings.migration_confirmation,
    allow_protected_database_access=settings.allow_protected_database_access,
    protected_database_confirmation=settings.protected_database_confirmation,
)
model_budget_service = None
legacy_budget_cutover = None
if settings.model_budget_strict_enabled:
    model_budget_service = ModelBudgetService(
        database,
        global_limit=(settings.model_daily_request_limit or 20) + settings.exploration_daily_model_request_limit,
        source_limits={"chat": settings.model_daily_request_limit or 20,
                      "contact": settings.model_daily_request_limit or 20,
                      "exploration": settings.exploration_daily_model_request_limit},
    )
    model_adapter = OpenAICompatibleAdapter(settings, strict_budget=True, budget_service=model_budget_service)
else:
    model_adapter = OpenAICompatibleAdapter(settings)
    if (settings.environment == "development" and settings.model_request_log_path
            and settings.exploration_model_request_log_path):
        legacy_budget_service = ModelBudgetService(database, global_limit=1)
        legacy_budget_cutover = ModelBudgetCutover(
            legacy_budget_service,
            files={"general": Path(settings.model_request_log_path),
                   "exploration": Path(settings.exploration_model_request_log_path)},
            inventory={},
        )
        model_adapter.legacy_cutover = legacy_budget_cutover

if desktop:
    class DesktopModelAdapter(OpenAICompatibleAdapter):
        async def _request_completion(self, messages, **kwargs):
            if kwargs.get("request_context") and kwargs.get("budget_context") is None:
                kwargs["budget_context"] = ModelBudgetContext(
                    uuid4().hex, current_character.get(), "exploration",
                    datetime.now(UTC).date().isoformat(),
                )
            live = OpenAICompatibleAdapter(
                desktop.model_settings(), strict_budget=True, budget_service=desktop.budget,
            )
            return await live._request_completion(messages, **kwargs)

    model_adapter = DesktopModelAdapter(settings, strict_budget=True, budget_service=desktop.budget)


def _new_exploration_agent(repository):
    if desktop:
        return ExplorationAgent(repository, life_repository, settings, model=model_adapter,
                                budget_service=desktop.budget)
    if settings.model_budget_strict_enabled:
        return ExplorationAgent(repository, life_repository, settings, model=model_adapter,
                                budget_service=model_budget_service)
    if legacy_budget_cutover is not None:
        return ExplorationAgent(repository, life_repository, settings, legacy_cutover=legacy_budget_cutover)
    return ExplorationAgent(repository, life_repository, settings)


conversations = ConversationRepository(database)
messages = MessageRepository(database)
message_delivery = MessageDeliveryRepository(database)
memory_service = MemoryService(MemoryRepository(database))
life_repository = LifeRepository(database, default_timezone=settings.life_timezone)
exploration_repository = ExplorationRepository(database, life_repository)
exploration_agent = _new_exploration_agent(exploration_repository)
active_exploration_tasks: dict[str, asyncio.Task] = {}
active_exploration_characters: dict[str, str] = {}
contact_generator = (
    AdapterContactGenerator(model_adapter, resolve_system_prompt(settings.system_prompt), life_repository)
    if settings.contact_model_generation_enabled else None
)
contact_service = ContactService(life_repository, generator=contact_generator, enabled=settings.contact_enabled)
desktop_contact = DesktopContact(desktop, life_repository, model_adapter) if desktop else None


class WebContactRuntime:
    """Adapt the existing web model policy to the shared contact orchestration."""
    def __init__(self, database, settings, model):
        self.database, self.settings, self.model = database, settings, model
        self.budget = self
        self.legacy_contact_budget = not model.strict_budget

    def require_autonomous_allowed(self, source):
        if not self.settings.contact_enabled:
            raise BudgetRejected('contact_globally_disabled')

    def status(self):
        return {'configured': bool(self.settings.model_enabled and self.settings.model_api_key)}

    def runtime_status(self):
        return {'remaining': None}

    def model_settings(self):
        return self.settings


web_contact = (AutonomousContact(WebContactRuntime(database, settings, model_adapter), life_repository, model_adapter)
               if not desktop and settings.contact_model_generation_enabled else None)
proposal_service = MemoryProposalService(MemoryProposalRepository(database))
memory_correction_service = MemoryCorrectionService(database)
correction_flow = CorrectionFlowService(memory_service, proposal_service)
clarification_repository = ClarificationRepository(database)
clarification_application = ClarificationApplicationService(
    clarification_repository, proposal_service, memory_correction_service
)
memory_retriever = MemoryRetriever(memory_service)
chat = ChatService(
    conversations,
    messages,
    model_adapter,
    resolve_system_prompt(settings.system_prompt),
    memory_service,
    DeterministicMemoryExtractor(),
    memory_retriever,
    MemoryContextBuilder(
        max_chars=settings.memory_context_max_chars,
        max_items=settings.memory_context_max_items,
        max_item_chars=settings.memory_item_max_chars,
    ),
    correction_flow=correction_flow,
    clarification_repository=clarification_repository,
    proposal_service=proposal_service,
    clarification_application=clarification_application,
    memory_correction_service=memory_correction_service,
    life_repository=life_repository,
    contact_service=contact_service,
    context_window=ModelContextWindow(ContextWindowLimits(
        max_input_tokens=settings.model_context_input_token_budget,
        max_persona_tokens=settings.model_context_persona_token_budget,
        max_expression_tokens=settings.model_context_expression_token_budget,
        max_clarification_tokens=settings.model_context_clarification_token_budget,
        max_system_control_tokens=settings.model_context_system_control_token_budget,
        max_current_message_tokens=settings.model_context_current_message_token_budget,
        max_history_tokens=settings.model_context_history_token_budget,
    )),
    runtime_features={
        "contact_background_enabled": settings.contact_background_enabled,
        "contact_model_generation_enabled": settings.contact_model_generation_enabled,
        **({"desktop_contact_status": desktop_contact.capability_status} if desktop_contact else {}),
    },
)


@asynccontextmanager
async def lifespan(application: FastAPI):
    global exploration_repository, exploration_agent
    exploration_repository = ExplorationRepository(database, life_repository)
    exploration_agent = _new_exploration_agent(exploration_repository)
    runtime = LifeInstance(database, life_repository)
    runtime.acquire()
    with database.connection() as db:
        exploration_characters = [row[0] for row in db.execute("SELECT id FROM characters WHERE ownership_status='owned'")]
    for character_id in exploration_characters:
        with character_scope(character_id):
            exploration_repository.recover_active(character_id)
    stop = asyncio.Event()
    tasks = []
    application.state.chat_worker = None
    worker = None
    contact_task = None
    try:
        if desktop:
            with character_scope("big_fish"):
                desktop.budget.recover("big_fish")
            # The exclusive desktop instance lock proves the last sync turn exited.
            with database.atomic() as db:
                db.execute("DELETE FROM chat_legacy_turns")
        life_repository.ensure_state()
        if desktop_contact:
            application.state.contact_settings_changed = asyncio.Event()
            contact_task = asyncio.create_task(
                desktop_contact.run(stop, application.state.contact_settings_changed), name="desktop-contact",
            )
        LifeShareService(life_repository).reconcile_recent()
        recovery_clarifications = ClarificationRepository(database)
        recovery_proposals = MemoryProposalService(MemoryProposalRepository(database))
        with database.connection() as db:
            owned_characters = [row[0] for row in db.execute("SELECT id FROM characters WHERE ownership_status='owned'")]
        for character_id in owned_characters:
            with character_scope(character_id):
                ClarificationApplicationService(
                    recovery_clarifications,
                    recovery_proposals,
                    MemoryCorrectionService(database),
                ).recover_after_restart()
        scheduler = LifeScheduler(life_repository, enabled=settings.life_background_enabled,
                                  interval=timedelta(minutes=settings.life_background_interval_minutes),
                                  after_tick=_autonomous_exploration_tick)
        if scheduler.enabled:
            tasks.append(asyncio.create_task(scheduler.run_forever(stop)))
        if not desktop and settings.contact_enabled and settings.contact_background_enabled:
            tasks.append(asyncio.create_task(ContactScheduler(
                web_contact or ContactService(life_repository, generator=contact_generator, enabled=settings.contact_enabled),
                interval=timedelta(minutes=settings.contact_check_interval_minutes),
            ).run_forever(stop)))
        if settings.autonomous_intent_shadow_enabled:
            tasks.append(asyncio.create_task(ShadowModeScheduler(
                database,
                enabled=True,
                contact_enabled=settings.contact_enabled,
                interval=timedelta(minutes=settings.autonomous_intent_shadow_interval_minutes),
            ).run_forever(stop)))
        tasks.append(asyncio.create_task(runtime.run_heartbeat(stop)))
        if settings.environment == "testing" and settings.chat_async_mock_enabled:
            mock_chat = copy(chat)
            mock_chat.model = DelayedMockChatModel(settings.chat_async_mock_delay_seconds)
            queue = ReplyQueue(database, conversation_id=settings.chat_async_conversation_id,
                               max_messages=settings.chat_async_max_messages, max_attempts=settings.chat_async_max_attempts,
                               max_output_tokens=settings.chat_async_max_output_tokens)
            worker = ChatReplyWorker(queue, mock_chat)
            worker.start(concurrency=1 if queue.conversation_id is not None else 2)
            application.state.chat_worker = worker
        if settings.chat_async_real_enabled or desktop:
            if (desktop or settings.environment == "development" and settings.model_budget_strict_enabled
                    and model_budget_service is not None and model_budget_service.ready(require_development_cutover=True)):
                queue = ReplyQueue(database, conversation_id=settings.chat_async_conversation_id,
                                   max_messages=settings.chat_async_max_messages,
                                   max_attempts=settings.chat_async_max_attempts,
                                   max_output_tokens=settings.chat_async_max_output_tokens)
                worker = ChatReplyWorker(queue, chat)
                worker.start(concurrency=1)
                application.state.chat_worker = worker
            else:
                logger.warning("chat_async_real_blocked reason=verified_budget_cutover_required")
        yield
    finally:
        stop.set()
        try:
            if contact_task:
                contact_task.cancel()
                await asyncio.gather(contact_task, return_exceptions=True)
            if worker is not None:
                await worker.drain(settings.chat_async_drain_seconds)
            for run_id, task in list(active_exploration_tasks.items()):
                character_id = active_exploration_characters.get(run_id, "big_fish")
                exploration_agent.stop(run_id, status="interrupted")
                with character_scope(character_id):
                    exploration_repository.terminate(run_id, character_id, "interrupted", error_code="service_stopped")
                task.cancel()
            if active_exploration_tasks:
                await asyncio.gather(*active_exploration_tasks.values(), return_exceptions=True)
            for task in tasks:
                await task
        finally:
            runtime.release()


app = FastAPI(
    title="Soulseed API", version="1.0.0", lifespan=lifespan,
    **({"docs_url": None, "redoc_url": None, "openapi_url": None} if desktop else {}),
)


if desktop:
    @app.exception_handler(RequestValidationError)
    async def desktop_validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "请求参数无效，请检查填写内容。"}, status_code=422)

    @app.get("/api/desktop/model")
    def desktop_model_status() -> dict:
        return desktop.status()

    @app.put("/api/desktop/model")
    async def desktop_model_save(request: ModelInput) -> dict:
        result = desktop.save(request)
        app.state.contact_settings_changed.set()
        return result

    @app.delete("/api/desktop/model")
    def desktop_model_clear() -> dict:
        return desktop.clear()

    @app.get("/api/desktop/runtime")
    def desktop_runtime_status() -> dict:
        worker = getattr(app.state, "chat_worker", None)
        return {**desktop.runtime_status(), "chat_worker_active": bool(worker and worker.accepting),
                "active_explorations": len(active_exploration_tasks), "contact_available": True}

    @app.put("/api/desktop/runtime")
    async def desktop_runtime_save(request: RuntimeLimitsInput) -> dict:
        desktop.save_runtime_limits(request)
        app.state.contact_settings_changed.set()
        return desktop_runtime_status()

    @app.post("/api/desktop/native/contact-notification")
    def desktop_contact_notification(foreground: bool) -> dict:
        return {"message_id": desktop_contact.consume_notification(foreground=foreground)}


@app.middleware("http")
async def authorize_local_request(request: Request, call_next):
    # Enforce loopback even if uvicorn is accidentally started with --host 0.0.0.0.
    try:
        peer_local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
    except ValueError:
        peer_local = False
    if not peer_local or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return JSONResponse({"detail": "当前服务仅允许本地回环访问"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in allowed_origins:
        return JSONResponse({"detail": "不允许的请求来源"}, status_code=403)
    path = request.url.path
    if desktop:
        supplied = request.headers.get("x-soulseed-desktop-token", "").encode()
        if not compare_digest(supplied, desktop.token.encode()):
            return JSONResponse({"detail": "桌面会话不可用"}, status_code=401)
        parts = path.split("/")
        if (path.startswith("/api/admin/") or path == "/api/auth/logout"
                or path == "/api/auth/session" and request.method != "GET"
                or len(parts) > 4 and parts[2] == "characters"
                and parts[4] == "contact" and parts[5:] != ["preferences"]):
            return JSONResponse({"detail": "此功能在当前 Windows 体验版中未启用"}, status_code=403)
    elif request.method == "OPTIONS" or not path.startswith("/api/") or path in {"/api/health", "/api/auth/session", "/api/auth/logout"}:
        return await call_next(request)
    try:
        if desktop or settings.environment == "development":
            # Development is loopback-only and binds every request to one
            # fixed local character; no client-supplied identity is accepted.
            principal = {"user_id": "local", "character_id": "big_fish", "expires_at": None}
        else:
            principal = LocalAuth(database, settings.local_pairing_secret).authenticate(request.cookies.get(COOKIE))
        parts = path.split("/")
        if len(parts) > 3 and parts[2] == "characters" and parts[3] != principal["character_id"]:
            raise HTTPException(403, "当前会话无权访问该角色")
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    request.state.principal = principal
    with character_scope(principal["character_id"]):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response


@app.middleware("http")
async def time_chat_request(request: Request, call_next):
    if request.method != "POST" or not request.url.path.startswith("/api/conversations/") or not request.url.path.endswith("/messages"):
        return await call_next(request)
    from .chat_diagnostics import ChatTrace, current_trace
    trace = ChatTrace()
    token = current_trace.set(trace)
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        trace.checkpoint(None)
        response.headers["X-Request-ID"] = trace.id
        response.headers["Server-Timing"] = ", ".join(
            f"{key};dur={value:.3f}" for key, value in trace.durations.items()
        )
        return response
    finally:
        trace.finish(status, settings.chat_timing_log_path)
        current_trace.reset(token)


app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(allowed_origins),
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=True,
    expose_headers=["X-Request-ID", "Server-Timing"],
)


@app.post("/api/auth/session")
def pair_local(request: PairRequest, response: Response) -> dict:
    if settings.environment == "development":
        raise HTTPException(404, "development 模式不使用配对登录")
    token, principal = LocalAuth(database, settings.local_pairing_secret).pair(request.secret, request.character_id)
    response.set_cookie(COOKIE, token, max_age=8 * 60 * 60, httponly=True, samesite="strict", path="/api")
    response.headers["Cache-Control"] = "no-store"
    return principal


@app.get("/api/auth/session")
def get_local_session(request: Request, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if desktop or settings.environment == "development":
        return {"user_id": "local", "character_id": "big_fish", "expires_at": None}
    return LocalAuth(database, settings.local_pairing_secret).authenticate(request.cookies.get(COOKIE))


@app.post("/api/auth/logout")
def logout_local(request: Request, response: Response) -> dict:
    if settings.environment == "development":
        return {"status": "disabled_in_development"}
    LocalAuth(database, settings.local_pairing_secret).logout(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/api")
    return {"status": "logged_out"}


def get_proposal_service() -> MemoryProposalService:
    return proposal_service


def get_memory_correction_service() -> MemoryCorrectionService:
    return memory_correction_service


app.include_router(create_proposal_router(get_proposal_service, get_memory_correction_service))


@app.get("/api/health")
def health() -> dict[str, str]:
    worker = getattr(app.state, "chat_worker", None)
    return {
        "status": "ok",
        "environment": settings.environment,
        "build_id": BUILD_ID,
        "startup_id": STARTUP_ID,
        "chat_reply_mode": ("real" if worker is not None and worker.accepting and (settings.chat_async_real_enabled or desktop)
                            else "mock" if worker is not None and worker.accepting else "disabled"),
        "chat_reply_blocker": ("verified_budget_cutover_required"
                               if settings.chat_async_real_enabled and worker is None else ""),
    }


def _launch_exploration(run_id: str, character_id: str) -> None:
    task = active_exploration_tasks.get(run_id)
    if task and not task.done():
        return
    task = asyncio.create_task(exploration_agent.run(run_id, character_id), name=f"exploration-{run_id}")
    active_exploration_tasks[run_id] = task
    active_exploration_characters[run_id] = character_id
    def clear(done, key=run_id):
        if active_exploration_tasks.get(key) is done:
            active_exploration_tasks.pop(key, None)
            active_exploration_characters.pop(key, None)
    task.add_done_callback(clear)


async def _autonomous_exploration_tick() -> None:
    if not settings.exploration_enabled:
        return
    if desktop and (desktop.budget.preferences()["autonomous_paused"] or not desktop.status()["configured"]):
        return
    with database.connection() as db:
        character_ids = [row[0] for row in db.execute("SELECT character_id FROM exploration_autonomy WHERE enabled=1")]
    for character_id in character_ids:
        with character_scope(character_id):
            run, created = exploration_repository.create_autonomous_if_due(
                character_id, quota_snapshot=exploration_agent.quota_snapshot)
            if created and run:
                logger.info("autonomous_exploration_started run_id=%s character_id=%s", run["id"], character_id)
                _launch_exploration(run["id"], character_id)


@app.post("/api/characters/{character_id}/explorations", status_code=202)
async def start_exploration(character_id: str, request: StartExplorationRequest) -> dict:
    topic = " ".join(request.topic.split()) if request.topic else None
    try:
        run, created = exploration_repository.create(character_id=character_id,
                                                     idempotency_key=request.idempotency_key,
                                                     requested_topic=topic,
                                                     enforce_single_active=True,
                                                     quota_snapshot=exploration_agent.quota_snapshot,
                                                     retry_run_id=request.retry_run_id,
                                                     autonomous_paused=bool(request.retry_run_id and desktop and
                                                                            desktop.budget.preferences()["autonomous_paused"]))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="探索记录不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (ModelDailyQuotaExhausted, ModelQuotaUnavailable) as exc:
        exhausted = isinstance(exc, ModelDailyQuotaExhausted)
        raise HTTPException(status_code=429 if exhausted else 503, detail={
            "code": "exploration_daily_quota_exhausted" if exhausted else "exploration_quota_unavailable",
            "message": str(exc), "quota": exploration_agent.quota_snapshot(),
        }) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if run["status"] == "planned":
        _launch_exploration(run["id"], character_id)
    return {**_exploration_view(run), "created": created}


def _exploration_view(run: dict) -> dict:
    diagnostics = exploration_agent.quota_adapter().request_diagnostics(run["id"])
    if run["model_call_count"] and diagnostics["reservations"] == 0 and run.get("error_code") not in {
        "exploration_daily_quota_exhausted", "exploration_quota_unavailable",
    }:
        # Legacy runs may predate correlated audit records; do not invent zero calls.
        diagnostics.update(reservations=None, http_attempts=None, http_attempts_complete=False)
    return {**run, "model_diagnostics": diagnostics}


@app.get("/api/characters/{character_id}/exploration-quota")
def get_exploration_quota(character_id: str) -> dict:
    return exploration_agent.quota_snapshot()


@app.get("/api/characters/{character_id}/exploration-autonomy")
def get_exploration_autonomy(character_id: str) -> dict:
    result = exploration_repository.autonomy_preferences(character_id)
    with database.connection() as db:
        active = db.execute("SELECT * FROM exploration_runs WHERE character_id=? AND status IN ('planned','active') ORDER BY created_at LIMIT 1", (character_id,)).fetchone()
        latest = db.execute("SELECT * FROM exploration_runs WHERE character_id=? ORDER BY created_at DESC LIMIT 1", (character_id,)).fetchone()
    result["current"] = _exploration_view(exploration_repository.get(active["id"], character_id)) if active else None
    result["latest"] = _exploration_view(exploration_repository.get(latest["id"], character_id)) if latest else None
    result["quota"] = exploration_agent.quota_snapshot()
    return result


@app.put("/api/characters/{character_id}/exploration-autonomy")
def set_exploration_autonomy(character_id: str, request: ExplorationAutonomyRequest) -> dict:
    fields = request.model_fields_set
    try:
        return exploration_repository.set_autonomy_preferences(
            character_id,
            enabled=request.enabled if "enabled" in fields else None,
            paused_until=request.paused_until.isoformat() if request.paused_until else None,
            clear_pause="paused_until" in fields and request.paused_until is None,
            cooldown_hours=request.cooldown_hours,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/characters/{character_id}/explorations")
def list_explorations(character_id: str, limit: int = 20) -> list[dict]:
    return [_exploration_view(run) for run in exploration_repository.list(character_id, limit)]


@app.get("/api/characters/{character_id}/explorations/{run_id}")
def get_exploration(character_id: str, run_id: str) -> dict:
    try:
        return _exploration_view(exploration_repository.get(run_id, character_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="探索记录不存在") from exc


@app.post("/api/characters/{character_id}/explorations/{run_id}/cancel")
async def cancel_exploration(character_id: str, run_id: str) -> dict:
    try:
        run = exploration_repository.get(run_id, character_id)
        if run["status"] in {"planned", "active"}:
            exploration_repository.terminate(run_id, character_id, "cancelled", error_code="cancelled_by_user")
            task = active_exploration_tasks.get(run_id)
            if task and not task.done():
                exploration_agent.stop(run_id, status="cancelled")
                task.cancel()
        return exploration_repository.get(run_id, character_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="探索记录不存在") from exc


def current_contact_service() -> ContactService:
    return ContactService(life_repository, generator=contact_generator, enabled=settings.contact_enabled)


@app.get("/api/characters/{character_id}/contact/preferences")
def get_contact_preferences(character_id: str) -> dict:
    return current_contact_service().preferences(character_id)


@app.post("/api/characters/{character_id}/contact/preferences")
def set_contact_preferences(character_id: str, request: ContactPreferencesRequest) -> dict:
    try:
        return current_contact_service().set_preferences(character_id, **request.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/characters/{character_id}/contact/intents")
def list_contact_intents(character_id: str) -> list[dict]:
    return current_contact_service().list_intents(character_id)


@app.post("/api/characters/{character_id}/contact/intents", status_code=201)
def create_contact_intent(character_id: str, request: ContactIntentRequest) -> dict:
    try:
        return current_contact_service().create_intent(character_id=character_id, **request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/characters/{character_id}/contact/inbox")
def get_contact_inbox(character_id: str) -> list[dict]:
    return current_contact_service().inbox(character_id)


@app.post("/api/characters/{character_id}/contact/check")
async def check_contact(character_id: str) -> dict:
    message = await current_contact_service().tick(character_id)
    return {"ready_message_id": message["id"] if message else None}


@app.get("/api/characters/{character_id}/contact/messages")
def list_contact_messages(character_id: str) -> list[dict]:
    return current_contact_service().list_messages(character_id)


@app.post("/api/characters/{character_id}/contact/messages/{message_id}/delivered")
def deliver_contact_message(character_id: str, message_id: str, request: DeliverContactRequest) -> dict:
    try:
        return current_contact_service().deliver(message_id, request.conversation_id, character_id=character_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="消息或会话不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/characters/{character_id}/contact/messages/{message_id}/read")
def read_contact_message(character_id: str, message_id: str) -> dict:
    try:
        return current_contact_service().mark_read(message_id, character_id=character_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="消息不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/characters/{character_id}/life")
def get_life(character_id: str) -> dict:
    LifeEngine(life_repository).advance(character_id=character_id)
    return {"state": life_repository.get_state(character_id), "events": life_repository.list_events(character_id),
            "pending_shares": LifeShareService(life_repository).list_intents(character_id, status="pending")}


@app.post("/api/characters/{character_id}/life/timezone")
def set_life_timezone(character_id: str, request: TimezoneRequest) -> dict:
    try:
        return life_repository.set_timezone(character_id, request.timezone_id)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(status_code=422, detail="无效或不可用的 IANA 时区") from exc


@app.get("/api/characters/{character_id}/life/shares")
def list_life_shares(character_id: str, status: Literal["pending", "delivered", "dismissed"] | None = None) -> list[dict]:
    return LifeShareService(life_repository).list_intents(character_id, status=status)


@app.post("/api/characters/{character_id}/life/shares/{intent_id}/transition")
def transition_life_share(character_id: str, intent_id: str, request: ShareTransitionRequest) -> dict:
    try:
        return LifeShareService(life_repository).transition(intent_id, request.target, character_id=character_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="分享意图不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/characters/{character_id}/life/advance")
def advance_life(character_id: str) -> dict:
    """Entry point for a low-frequency external scheduler or an explicit tick."""
    changes = LifeEngine(life_repository).advance(character_id=character_id)
    return {"changes": changes, "state": life_repository.get_state(character_id)}


@app.post("/api/characters/{character_id}/life/events", status_code=201)
def record_life_event(character_id: str, request: LifeEventRequest) -> dict:
    try:
        event = life_repository.record(**request.model_dump(), character_id=character_id)
        if event["activity_status"] == "completed":
            LifeEngine(life_repository).learn_from_completion(event, datetime.now(UTC))
            LifeShareService(life_repository).consider(event["id"])
        return life_repository.present_event(event)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/characters/{character_id}/life/events/{event_id}/transition")
def transition_life_event(character_id: str, event_id: str, request: ActivityTransitionRequest) -> dict:
    try:
        event = life_repository.transition(event_id, request.target, character_id=character_id, reason=request.reason, feeling=request.feeling)
        if request.target == "completed":
            LifeEngine(life_repository).learn_from_completion(event, datetime.now(UTC))
            LifeShareService(life_repository).consider(event["id"])
        return life_repository.present_event(event)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="活动不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/conversations")
def list_conversations() -> list[dict]:
    return conversations.list()


@app.post("/api/conversations", status_code=201)
def create_conversation(request: CreateConversationRequest) -> dict:
    return conversations.create(request.title)


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict:
    try:
        conversation = conversations.get(conversation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    return {**conversation, "messages": messages.list_for_conversation(conversation_id)}


@app.post("/api/conversations/{conversation_id}/user-messages", status_code=201)
def save_user_message(conversation_id: str, request: IndependentMessageRequest, http_request: Request) -> dict:
    principal = http_request.state.principal
    def admit(db, conversation_id: str) -> None:
        worker = getattr(http_request.app.state, "chat_worker", None)
        if worker is None:
            raise HTTPException(503, {"code": "async_chat_unavailable", "message": "异步回复当前不可用，消息未保存"})
        worker.admit(db, conversation_id)

    return message_delivery.submit(
        user_id=principal["user_id"], character_id=principal["character_id"],
        conversation_id=conversation_id, client_message_id=request.client_message_id,
        client_stream_id=request.client_stream_id, client_sequence=request.client_sequence,
        content=request.content, admit=admit,
    )


@app.get("/api/conversations/{conversation_id}/user-messages/{client_message_id}")
def get_user_message_submission(conversation_id: str, client_message_id: str, http_request: Request) -> dict:
    principal = http_request.state.principal
    return message_delivery.lookup(
        user_id=principal["user_id"], character_id=principal["character_id"],
        conversation_id=conversation_id, client_message_id=client_message_id,
    )


@app.post("/api/conversations/{conversation_id}/messages")
async def send_message(conversation_id: str, request: SendMessageRequest) -> dict:
    try:
        conversations.get(conversation_id)
        service = chat
        if desktop:
            service = copy(chat)
            service.model = OpenAICompatibleAdapter(
                desktop.model_settings(), strict_budget=True, budget_service=desktop.budget,
            )
        if (settings.model_conversation_turn_limit is not None
                and sum(item["role"] == "user" for item in messages.list_for_conversation(conversation_id))
                >= settings.model_conversation_turn_limit):
            raise HTTPException(status_code=429, detail="当前会话已达到开发环境聊天轮次上限；请新建会话。")
        with ReplyQueue(conversations.database).legacy_turn(conversation_id) as execution:
            with reply_execution(execution):
                return await service.send(conversation_id, request.content.strip())
    except ConversationReplyBusy as exc:
        raise HTTPException(409, {"code": "conversation_reply_pending", "message": str(exc)}) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except BudgetRejected as exc:
        if not desktop:
            return JSONResponse(status_code=502, content={"detail": str(exc), "message_saved": True})
        exhausted = str(exc) in {"global_budget_exhausted", "source_budget_exhausted"}
        return JSONResponse(
            status_code=429 if exhausted else 503,
            content={"detail": "今日模型请求额度已用完（UTC 午夜重置）。" if exhausted else "无法核验模型请求额度。",
                     "message_saved": True},
        )
    except ModelConfigurationError as exc:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc), "message_saved": True},
        )
    except ModelRequestError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc), "message_saved": exc.message_saved},
        )
    except ContextBudgetExceeded as exc:
        return JSONResponse(
            status_code=413,
            content={"detail": str(exc), "message_saved": exc.message_saved},
        )
    except RuntimeError as exc:
        return JSONResponse(
            status_code=502,
            content={"detail": str(exc), "message_saved": True},
        )


@app.get("/api/conversations/{conversation_id}/messages")
def get_message_feed(conversation_id: str, request: Request,
                     after_sequence: int = Query(0, ge=0, le=2**63 - 1),
                     limit: int = Query(100, ge=1, le=200)) -> dict:
    principal = request.state.principal
    feed = message_delivery.feed(
        user_id=principal["user_id"], character_id=principal["character_id"],
        conversation_id=conversation_id, after_sequence=after_sequence, limit=limit,
    )
    worker = getattr(request.app.state, "chat_worker", None)
    feed["async_chat_accepting"] = bool(worker and worker.accepting
                                         and worker.queue.conversation_id in (None, conversation_id))
    return feed


def chat_operator(request: Request) -> dict:
    secret = settings.chat_operator_secret
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if not secret or scheme.lower() != "bearer" or not compare_digest(value.encode(), secret.encode()):
        raise HTTPException(401, "需要有效的聊天管理凭据", headers={"WWW-Authenticate": "Bearer"})
    return request.state.principal


@app.get("/api/admin/chat/replies/{message_id}")
def inspect_chat_reply(message_id: str, request: Request) -> dict:
    actor = chat_operator(request)
    return ChatReplyReview(database).inspect(message_id, actor["user_id"])


@app.post("/api/admin/chat/replies/{message_id}/resolution")
def resolve_chat_reply(message_id: str, review: ChatReplyReviewRequest, request: Request) -> dict:
    actor = chat_operator(request)
    return ChatReplyReview(database).resolve(message_id, actor["user_id"], **review.model_dump())


@app.post("/api/admin/chat/drain")
async def drain_chat(request: Request) -> dict:
    chat_operator(request)
    worker = getattr(request.app.state, "chat_worker", None)
    if worker is not None:
        await worker.drain(settings.chat_async_drain_seconds)
    return {"accepting": False}

