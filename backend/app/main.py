import asyncio
import ipaddress
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfoNotFoundError

from .chat_service import ChatService
from .config import get_settings
from .db import Database
from .model_adapter import ModelConfigurationError, ModelRequestError, OpenAICompatibleAdapter
from .memory import ClarificationApplicationService, ClarificationRepository, CorrectionFlowService, DeterministicMemoryExtractor, MemoryContextBuilder, MemoryCorrectionService, MemoryProposalRepository, MemoryProposalService, MemoryRepository, MemoryRetriever, MemoryService
from .persona import resolve_system_prompt
from .proposal_api import create_proposal_router
from .repositories import ConversationRepository, MessageRepository
from .life import LifeRepository
from .life_engine import LifeEngine
from .life_scheduler import LifeScheduler
from .life_instance import LifeInstance
from .life_share import LifeShareService
from .contact import AdapterContactGenerator, ContactService
from .contact_scheduler import ContactScheduler
from .local_auth import LocalAuth, COOKIE, ALLOWED_ORIGINS
from .ownership import character_scope


class CreateConversationRequest(BaseModel):
    title: str = Field(default="新会话", min_length=1, max_length=120)


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


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


settings = get_settings()
allowed_origins = set(ALLOWED_ORIGINS)
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
database = Database(
    settings.database_path,
    environment=settings.environment,
    test_data_root=settings.test_data_root,
    migration_backup_path=settings.migration_backup_path,
    migration_confirmation=settings.migration_confirmation,
    allow_protected_database_access=settings.allow_protected_database_access,
    protected_database_confirmation=settings.protected_database_confirmation,
)
conversations = ConversationRepository(database)
messages = MessageRepository(database)
memory_service = MemoryService(MemoryRepository(database))
life_repository = LifeRepository(database, default_timezone=settings.life_timezone)
model_adapter = OpenAICompatibleAdapter(settings)
contact_generator = (
    AdapterContactGenerator(model_adapter, resolve_system_prompt(settings.system_prompt), life_repository)
    if settings.contact_model_generation_enabled else None
)
contact_service = ContactService(life_repository, generator=contact_generator, enabled=settings.contact_enabled)
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
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    runtime = LifeInstance(database, life_repository)
    runtime.acquire()
    stop = asyncio.Event()
    tasks = []
    try:
        life_repository.ensure_state()
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
                                  interval=timedelta(minutes=settings.life_background_interval_minutes))
        if scheduler.enabled:
            tasks.append(asyncio.create_task(scheduler.run_forever(stop)))
        if settings.contact_enabled and settings.contact_background_enabled:
            tasks.append(asyncio.create_task(ContactScheduler(
                ContactService(life_repository, enabled=settings.contact_enabled),
                interval=timedelta(minutes=settings.contact_check_interval_minutes),
            ).run_forever(stop)))
        tasks.append(asyncio.create_task(runtime.run_heartbeat(stop)))
        yield
    finally:
        stop.set()
        try:
            for task in tasks:
                await task
        finally:
            runtime.release()


app = FastAPI(title="Soulseed API", version="1.0.0", lifespan=lifespan)


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
    if request.method == "OPTIONS" or not path.startswith("/api/") or path in {"/api/health", "/api/auth/session", "/api/auth/logout"}:
        return await call_next(request)
    try:
        if settings.environment == "development":
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


app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(allowed_origins),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=True,
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
    if settings.environment == "development":
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
    return {"status": "ok"}


def current_contact_service() -> ContactService:
    return ContactService(life_repository, enabled=settings.contact_enabled)


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


@app.post("/api/conversations/{conversation_id}/messages")
async def send_message(conversation_id: str, request: SendMessageRequest) -> dict:
    try:
        conversations.get(conversation_id)
        if (settings.model_conversation_turn_limit is not None
                and sum(item["role"] == "user" for item in messages.list_for_conversation(conversation_id))
                >= settings.model_conversation_turn_limit):
            raise HTTPException(status_code=429, detail="当前会话已达到开发环境聊天轮次上限；请新建会话。")
        return await chat.send(conversation_id, request.content.strip())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except ModelConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ModelRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

