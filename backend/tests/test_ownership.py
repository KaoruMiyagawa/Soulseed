"""Ownership, authorization and legacy migration regression tests (isolated DBs)."""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

import app.db as db_module
import app.main as main
from app.chat_service import ChatService
from app.context_window import ContextWindowLimits, ModelContextWindow
from app.config import Settings
from app.contact import ContactService
from app.db import Database
from app.life import LifeRepository
from app.local_auth import COOKIE, LocalAuth, digest
from app.memory import (
    ClarificationRepository, ClarificationStateError, MemoryCreate, MemoryRepository,
    MemoryService, MemoryType, MemoryProposalRepository, MemoryProposalService,
    MemoryCorrectionService, ProposalCreate, ProposalType, CorrectionIntent,
    MemoryRetriever, MemoryContextBuilder,
)
from app.memory.repositories import MemoryNotFoundError, MessageNotFoundError
from app.memory.proposal_repositories import ProposalNotFoundError
from app.memory.clarification_repository import ClarificationNotFoundError
from app.ownership import character_scope, current_character
from app.repositories import ConversationRepository, MessageRepository
from .conftest import TEST_PAIRING_SECRET


@pytest.fixture
def owned(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "owned.db")); db.initialize()
    with db.connection() as c:
        c.execute("INSERT INTO users VALUES ('bob', datetime('now'))")
        c.execute("INSERT INTO characters VALUES ('fish_b', 'local', 'owned', datetime('now'))")
        c.execute("INSERT INTO characters VALUES ('fish_c', 'bob', 'owned', datetime('now'))")
    conversations, messages = ConversationRepository(db), MessageRepository(db)
    memories = MemoryService(MemoryRepository(db))
    records = {}
    for char in ("big_fish", "fish_b", "fish_c"):
        with character_scope(char):
            conv = conversations.create(char)
            msg = messages.add(conv["id"], "user", f"最近在听音乐，{char} 的独立话题")
            memory = memories.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, f"喜欢音乐：{char}专属秘密", "user:music"), idempotency_key="same-key")
            records[char] = (conv, msg, memory)
    monkeypatch.setattr(main, "database", db)
    monkeypatch.setattr(main, "conversations", conversations)
    monkeypatch.setattr(main, "messages", messages)
    monkeypatch.setattr(main, "life_repository", LifeRepository(db))
    monkeypatch.setattr(main, "proposal_service", MemoryProposalService(MemoryProposalRepository(db)))
    monkeypatch.setattr(main, "memory_correction_service", MemoryCorrectionService(db))
    return db, records, memories


def raw_client(**kwargs):
    return TestClient(main.app, base_url="http://127.0.0.1:8001", client=kwargs.pop("client", ("127.0.0.1", 12345)), **kwargs)


def paired(char="big_fish"):
    client = raw_client()
    result = client.post("/api/auth/session", json={"secret": TEST_PAIRING_SECRET, "character_id": char})
    assert result.status_code == 200, result.text
    return client


def proposal_for(msg, memory):
    return ProposalCreate(
        proposal_type=ProposalType.CORRECTION, target_memory_id=memory.id,
        proposed_memory_type=MemoryType.USER_PREFERENCE, proposed_content="现在喜欢安静的音乐",
        proposed_canonical_key="user:music:quiet", evidence_message_id=msg["id"],
        correction_intent=CorrectionIntent.EXPLICIT_CORRECTION, relation="conflict",
        confidence=0.9, evidence="用户明确改变偏好", idempotency_key="same-proposal-key",
    )


def test_all_data_endpoints_require_identity(owned):
    db, records, _ = owned
    conv = records["big_fish"][0]
    client = raw_client()
    for path in ("/api/conversations", f"/api/conversations/{conv['id']}", "/api/proposals",
                 "/api/characters/big_fish/life", "/api/characters/big_fish/contact/inbox"):
        assert client.get(path).status_code == 401
    assert client.post("/api/characters/big_fish/life/events", json={"content": "forged"}).status_code == 401
    with db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM life_events").fetchone()[0] == 0


def test_development_conversation_turn_limit_rejects_before_model(owned, monkeypatch):
    _, records, _ = owned
    conversation_id = records["big_fish"][0]["id"]
    monkeypatch.setattr(main.settings, "model_conversation_turn_limit", 1)
    client = paired()
    response = client.post(f"/api/conversations/{conversation_id}/messages", json={"content": "another turn"})
    assert response.status_code == 429
    with character_scope("big_fish"):
        assert [item["content"] for item in main.messages.list_for_conversation(conversation_id)] == [
            records["big_fish"][1]["content"]
        ]


def test_oversized_current_message_returns_413_without_persisting_retryable_input(owned, monkeypatch):
    _, records, _ = owned
    _, conversations, messages = main.database, main.conversations, main.messages
    calls = []

    class Model:
        async def reply(self, _messages):
            calls.append("reply")
            return "should not run"

    chat = ChatService(
        conversations,
        messages,
        Model(),
        "人格",
        context_window=ModelContextWindow(ContextWindowLimits(
            max_input_tokens=3000,
            max_persona_tokens=500,
            max_expression_tokens=500,
            max_clarification_tokens=500,
            max_system_control_tokens=500,
            max_current_message_tokens=128,
            max_history_tokens=500,
        )),
    )
    monkeypatch.setattr(main, "chat", chat)
    monkeypatch.setattr(main.settings, "model_conversation_turn_limit", None)
    conversation_id = records["big_fish"][0]["id"]
    client = paired()
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "超长消息" * 100},
    )
    assert response.status_code == 413
    assert response.json()["message_saved"] is False
    assert calls == []
    with character_scope("big_fish"):
        assert [item["content"] for item in messages.list_for_conversation(conversation_id)] == [
            records["big_fish"][1]["content"]
        ]


def test_required_context_413_is_explicitly_marked_as_persisted(owned, monkeypatch):
    _, records, _ = owned
    _, conversations, messages = main.database, main.conversations, main.messages
    calls = []

    class Model:
        async def reply(self, _messages):
            calls.append("reply")
            return "unexpected"

    chat = ChatService(
        conversations,
        messages,
        Model(),
        "人格" * 200,
        context_window=ModelContextWindow(ContextWindowLimits(
            max_input_tokens=100,
            max_persona_tokens=500,
            max_expression_tokens=500,
            max_clarification_tokens=500,
            max_system_control_tokens=500,
            max_current_message_tokens=128,
            max_history_tokens=500,
        )),
    )
    monkeypatch.setattr(main, "chat", chat)
    monkeypatch.setattr(main.settings, "model_conversation_turn_limit", None)
    conversation_id = records["big_fish"][0]["id"]
    response = paired().post(
        f"/api/conversations/{conversation_id}/messages", json={"content": "短消息"}
    )
    assert response.status_code == 413
    assert response.json()["message_saved"] is True
    assert "不要原样重发" in response.json()["detail"]
    assert calls == []
    with character_scope("big_fish"):
        assert messages.list_for_conversation(conversation_id)[-1]["content"] == "短消息"


def test_development_loopback_uses_only_big_fish_without_pairing(owned, monkeypatch):
    _, records, _ = owned
    monkeypatch.setattr(main.settings, "environment", "development")
    client = raw_client()
    assert client.get("/api/auth/session").status_code == 200
    assert client.get("/api/auth/session").json()["character_id"] == "big_fish"
    assert client.get(f"/api/conversations/{records['big_fish'][0]['id']}").status_code == 200
    assert client.get(f"/api/conversations/{records['fish_b'][0]['id']}").status_code == 404
    assert client.post("/api/auth/session", json={"secret": "unused", "character_id": "big_fish"}).status_code == 404


def test_forged_role_conversation_and_owner_cannot_authorize(owned):
    db, records, _ = owned
    client = paired()
    bconv = records["fish_b"][0]
    assert client.get(f"/api/conversations/{bconv['id']}").status_code == 404
    assert client.post(f"/api/conversations/{bconv['id']}/messages", json={"content": "forged"}).status_code == 404
    assert client.get("/api/characters/fish_b/life").status_code == 403
    assert client.post("/api/characters/fish_b/contact/preferences", json={"allowed": False}).status_code == 403
    created = client.post("/api/conversations", json={"title": "server-selected", "character_id": "fish_b", "owner_id": "bob"})
    assert created.status_code == 201 and created.json()["character_id"] == "big_fish"
    assert raw_client().post("/api/auth/session", json={"secret": TEST_PAIRING_SECRET, "character_id": "fish_c", "user_id": "bob", "owner_id": "bob"}).status_code == 403
    assert paired("fish_b").get(f"/api/conversations/{bconv['id']}").status_code == 200
    with db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM life_states WHERE character_id='fish_b'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM messages WHERE content='forged'").fetchone()[0] == 0


def test_pair_cookie_expiry_revocation_and_restart(owned):
    db, _, _ = owned
    client = paired()
    token = client.cookies.get(COOKIE)
    with db.connection() as c:
        stored = c.execute("SELECT token_hash FROM auth_sessions").fetchone()[0]
        assert stored == digest(token) and stored != token
    assert LocalAuth(Database(db.path), TEST_PAIRING_SECRET).authenticate(token)["character_id"] == "big_fish"
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/conversations").status_code == 401
    with pytest.raises(HTTPException): LocalAuth(db, TEST_PAIRING_SECRET).authenticate(token)
    client = paired()
    with db.connection() as c: c.execute("UPDATE auth_sessions SET expires_at='2000-01-01'")
    assert client.get("/api/conversations").status_code == 401


def test_pair_cookie_flags_and_rate_limit(owned):
    db, _, _ = owned
    client = raw_client()
    result = client.post("/api/auth/session", json={"secret": TEST_PAIRING_SECRET})
    cookie = result.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/api" in cookie
    for _ in range(5): assert client.post("/api/auth/session", json={"secret": "wrong"}).status_code == 401
    assert client.post("/api/auth/session", json={"secret": TEST_PAIRING_SECRET}).status_code == 429
    with db.connection() as c:
        assert c.execute("SELECT failures FROM auth_login_throttle").fetchone()[0] == 5
        assert c.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 1


def test_network_origin_and_host_boundaries(owned):
    client = paired()
    assert client.get("/api/conversations", headers={"Origin": "https://untrusted.example"}).status_code == 403
    assert client.get("/api/conversations", headers={"Host": "untrusted.example"}).status_code == 403
    remote = raw_client(client=("192.0.2.1", 12345))
    remote.cookies.update(client.cookies)
    assert remote.get("/api/conversations").status_code == 403
    with pytest.raises(ValueError): Settings(host="0.0.0.0")


def test_missing_pairing_secret_fails_closed(owned, monkeypatch):
    client = paired()
    monkeypatch.setattr(main.settings, "local_pairing_secret", None)
    assert client.get("/api/conversations").status_code == 401
    assert client.post("/api/auth/session", json={"secret": TEST_PAIRING_SECRET}).status_code == 503


def test_scoped_memory_reads_writes_and_sources(owned):
    db, records, service = owned
    bconv, bmsg, bmem = records["fish_b"]
    assert [x.id for x in service.list_memories()] == [records["big_fish"][2].id]
    with pytest.raises(MemoryNotFoundError): service.get_memory(bmem.id)
    with pytest.raises(KeyError): MessageRepository(db).add(bconv["id"], "user", "no")
    with pytest.raises(MessageNotFoundError): MemoryRepository(db).add_source(records["big_fish"][2].id, bmsg["id"], "user", "explicit_fact")
    with db.connection() as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO memory_sources(id,memory_id,message_id,source_role,evidence_type,created_at,character_id) VALUES ('bad',?,?,'user','explicit_fact',datetime('now'),'big_fish')", (records["big_fish"][2].id,bmsg["id"]))
    with character_scope("fish_b"):
        assert service.get_memory(bmem.id).content.endswith("fish_b专属秘密")


def test_proposal_application_and_clarification_stay_in_character(owned):
    db, records, service = owned
    proposals = MemoryProposalService(MemoryProposalRepository(db))
    with character_scope("fish_b"):
        bconv, bmsg, bmem = records["fish_b"]
        proposal = proposals.create_proposal(proposal_for(bmsg,bmem))
        proposals.accept_proposal(proposal.id)
        context = {"candidate": {"memory_type": "user_preference", "content": bmsg["content"], "canonical_key": "user:music"},
                   "matched_memories": [{"id": bmem.id,"content": bmem.content,"updated_at": bmem.updated_at}],
                   "relation": "potential_conflict", "evidence": ["真实消息"], "reason": "澄清偏好"}
        clarification = ClarificationRepository(db).create(bconv["id"],bmsg["id"],context)
    client=paired()
    assert client.get(f"/api/proposals/{proposal.id}").status_code == 404
    assert client.post(f"/api/proposals/{proposal.id}/apply",json={}).status_code == 404
    with pytest.raises(ProposalNotFoundError): MemoryCorrectionService(db).apply_proposal(proposal.id)
    with pytest.raises(ClarificationNotFoundError): ClarificationRepository(db).get(clarification.id)
    assert ClarificationRepository(db).list_recoverable() == []
    with character_scope("fish_b"):
        result = MemoryCorrectionService(db).apply_proposal(proposal.id)
        assert result is not None
        assert service.get_memory(bmem.id, include_inactive=True).status.value == "superseded"


def test_latest_message_followup_and_delivery_do_not_cross_roles(owned):
    db, records, _ = owned
    now = datetime(2026,11,2,3,tzinfo=UTC)
    for char, (_, msg, _) in records.items():
        with character_scope(char), db.connection() as c:
            at = now if char == "fish_b" else now-timedelta(hours=30)
            c.execute("UPDATE messages SET created_at=? WHERE id=?", (at.isoformat(), msg["id"]))
    service=ContactService(LifeRepository(db))
    intent=service.maybe_create_smalltalk("big_fish",now=now)
    assert intent["source_id"] == records["big_fish"][1]["id"]
    assert service.maybe_create_smalltalk("fish_b",now=now) is None
    with pytest.raises(ValueError): service.create_intent(character_id="big_fish",source="smalltalk",source_id=records["fish_b"][1]["id"],reason="forged",now=now)
    ready=asyncio.run(service.tick("big_fish",now=now))
    assert ready is not None  # B's recent message did not suppress A.
    with pytest.raises(KeyError): service.deliver(ready["id"],records["fish_b"][0]["id"],now=now)
    with db.connection() as c:
        assert c.execute("SELECT status FROM proactive_messages WHERE id=?",(ready["id"],)).fetchone()[0]=='ready'
        assert c.execute("SELECT COUNT(*) FROM messages WHERE id=?",(ready["id"],)).fetchone()[0]==0


@pytest.mark.asyncio
async def test_chat_retrieval_and_life_use_server_scope(owned):
    db, records, service=owned
    class Capture:
        async def reply(self,messages):
            self.prompt=json.dumps(messages,ensure_ascii=False)
            return "收到。"
    model=Capture()
    chat=ChatService(ConversationRepository(db),MessageRepository(db),model,"人格",memory_service=service,
        memory_retriever=MemoryRetriever(service),memory_context_builder=MemoryContextBuilder(),
        life_repository=LifeRepository(db),contact_service=ContactService(LifeRepository(db)))
    with character_scope("fish_b"):
        await chat.send(records["fish_b"][0]["id"],"喜欢音乐")
    assert "fish_b专属秘密" in model.prompt and "big_fish专属秘密" not in model.prompt
    with db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM life_events WHERE character_id='fish_b'").fetchone()[0]==1
        assert c.execute("SELECT COUNT(*) FROM life_events WHERE character_id='big_fish'").fetchone()[0]==0


def test_parallel_http_requests_keep_character_scope(owned):
    _, records, _=owned
    clients={char:paired(char) for char in ("big_fish","fish_b")}
    def read(char):
        for _ in range(5):
            data=clients[char].get("/api/conversations").json()
            assert [x["id"] for x in data]==[records[char][0]["id"]]
    with ThreadPoolExecutor(max_workers=2) as pool: list(pool.map(read,clients))
    assert current_character.get()=="big_fish"


def test_http_default_completed_time_and_future_validation(owned):
    db, _, _=owned
    client=paired()
    result=client.post("/api/characters/big_fish/life/events",json={"content":"已经完成的阅读"})
    assert result.status_code==201,result.text
    row=result.json()
    assert row["occurred_at"]==row["created_at"] and row["activity_status"]=="completed"
    future=datetime.now(UTC)+timedelta(hours=1)
    assert client.post("/api/characters/big_fish/life/events",json={"content":"未来事件","occurred_at":future.isoformat()}).status_code==422
    with db.connection() as c: assert c.execute("SELECT COUNT(*) FROM life_events").fetchone()[0]==1
