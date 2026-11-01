from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .chat_service import ChatService
from .config import get_settings
from .db import Database
from .model_adapter import ModelConfigurationError, ModelRequestError, OpenAICompatibleAdapter
from .memory import CorrectionFlowService, DeterministicMemoryExtractor, MemoryContextBuilder, MemoryCorrectionService, MemoryProposalRepository, MemoryProposalService, MemoryRepository, MemoryRetriever, MemoryService
from .persona import resolve_system_prompt
from .proposal_api import create_proposal_router
from .repositories import ConversationRepository, MessageRepository


class CreateConversationRequest(BaseModel):
    title: str = Field(default="新会话", min_length=1, max_length=120)


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


settings = get_settings()
database = Database(settings.database_path)
conversations = ConversationRepository(database)
messages = MessageRepository(database)
memory_service = MemoryService(MemoryRepository(database))
proposal_service = MemoryProposalService(MemoryProposalRepository(database))
memory_correction_service = MemoryCorrectionService(database)
correction_flow = CorrectionFlowService(memory_service, proposal_service)
memory_retriever = MemoryRetriever(memory_service)
chat = ChatService(
    conversations,
    messages,
    OpenAICompatibleAdapter(settings),
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
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    yield


app = FastAPI(title="Soulseed API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5174", "http://localhost:5174"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


def get_proposal_service() -> MemoryProposalService:
    return proposal_service


def get_memory_correction_service() -> MemoryCorrectionService:
    return memory_correction_service


app.include_router(create_proposal_router(get_proposal_service, get_memory_correction_service))


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
        return await chat.send(conversation_id, request.content.strip())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except ModelConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ModelRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

