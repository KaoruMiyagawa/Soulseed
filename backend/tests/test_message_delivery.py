from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.db import Database
from app.ownership import character_scope
from app.repositories import ConversationRepository, MessageRepository
from .conftest import TEST_PAIRING_SECRET


@pytest.fixture
def delivery_env(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "delivery.db"))
    db.initialize()
    conversations = ConversationRepository(db)
    with db.connection() as sql:
        sql.execute("INSERT INTO characters VALUES ('fish_b','local','owned',datetime('now'))")
    with character_scope("big_fish"):
        conv_a = conversations.create("A")
        conv_b = conversations.create("B")
    with character_scope("fish_b"):
        conv_c = conversations.create("C")
    monkeypatch.setattr(main, "database", db)
    monkeypatch.setattr(main, "conversations", conversations)
    monkeypatch.setattr(main, "messages", MessageRepository(db))
    monkeypatch.setattr(main, "message_delivery", main.MessageDeliveryRepository(db))
    monkeypatch.setattr(main.app.state, "chat_worker", SimpleNamespace(admit=lambda _db, _conversation: None), raising=False)
    monkeypatch.setattr(main.settings, "local_pairing_secret", TEST_PAIRING_SECRET)
    return db, conv_a, conv_b, conv_c


def client(character="big_fish"):
    result = TestClient(main.app, base_url="http://127.0.0.1:8001", client=("127.0.0.1", 12400))
    paired = result.post("/api/auth/session", json={"secret": TEST_PAIRING_SECRET, "character_id": character})
    assert paired.status_code == 200, paired.text
    return result


def payload(message_id="client-message-0001", stream="client-stream-0001", sequence=1, content="独立保存"):
    return {"client_message_id": message_id, "client_stream_id": stream,
            "client_sequence": sequence, "content": content}


def test_independent_save_returns_without_model_or_memory_work(delivery_env, monkeypatch):
    db, conversation, _, _ = delivery_env

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("model path called")

    monkeypatch.setattr(main.chat, "send", forbidden)
    response = client().post(f"/api/conversations/{conversation['id']}/user-messages", json=payload())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["save_status"] == "saved"
    assert body["role_status"] == "waiting"
    assert body["server_sequence"] == 1
    with db.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert sql.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0
        assert sql.execute("SELECT COUNT(*) FROM memory_correction_clarifications").fetchone()[0] == 0


def test_retry_same_id_and_lost_confirmation_lookup_recover_one_message(delivery_env):
    db, conversation, _, _ = delivery_env
    api = client()
    path = f"/api/conversations/{conversation['id']}/user-messages"
    first = api.post(path, json=payload())
    retry = api.post(path, json=payload())
    lookup = api.get(f"{path}/client-message-0001")
    assert first.status_code == retry.status_code == 201
    assert retry.json()["duplicate"] is True
    assert lookup.status_code == 200
    assert lookup.json()["message_id"] == first.json()["message_id"] == retry.json()["message_id"]
    with db.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert sql.execute("SELECT COUNT(*) FROM client_message_submissions").fetchone()[0] == 1


def test_id_content_stream_and_conversation_reuse_are_rejected(delivery_env):
    _, first_conversation, second_conversation, _ = delivery_env
    api = client()
    endpoint = f"/api/conversations/{first_conversation['id']}/user-messages"
    assert api.post(endpoint, json=payload()).status_code == 201
    changed = api.post(endpoint, json=payload(content="不同正文"))
    assert changed.status_code == 409
    reused_in_conversation = api.post(
        f"/api/conversations/{second_conversation['id']}/user-messages", json=payload()
    )
    assert reused_in_conversation.status_code == 409
    reused_stream = api.post(
        f"/api/conversations/{second_conversation['id']}/user-messages",
        json=payload(message_id="different-message-0001", stream="client-stream-0001"),
    )
    assert reused_stream.status_code == 409


def test_second_message_cannot_cross_unconfirmed_first_sequence(delivery_env):
    db, conversation, _, _ = delivery_env
    api = client()
    endpoint = f"/api/conversations/{conversation['id']}/user-messages"
    second = payload("client-message-0002", sequence=2, content="第二条")
    blocked = api.post(endpoint, json=second)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["expected_client_sequence"] == 1
    first = api.post(endpoint, json=payload())
    retried_second = api.post(endpoint, json=second)
    assert first.status_code == retried_second.status_code == 201
    assert retried_second.json()["server_sequence"] == first.json()["server_sequence"] + 1
    with db.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_streams_roles_and_characters_are_isolated(delivery_env):
    db, conversation_a, _, conversation_b = delivery_env
    first = client("big_fish").post(
        f"/api/conversations/{conversation_a['id']}/user-messages",
        json={**payload(), "role": "assistant"},
    )
    same_id_other_role_or_character = client("fish_b").post(
        f"/api/conversations/{conversation_b['id']}/user-messages",
        json=payload(stream="another-client-stream-001"),
    )
    assert first.status_code == 201
    assert first.json()["role"] == "user"
    # The ID remains bound to the original character and conversation.
    assert same_id_other_role_or_character.status_code == 409
    with db.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM client_message_submissions").fetchone()[0] == 1


def test_concurrent_streams_get_unique_server_order_and_same_id_is_atomic(delivery_env):
    db, conversation, _, _ = delivery_env

    def submit(args):
        message_id, stream_id = args
        with character_scope("big_fish"):
            return main.message_delivery.submit(
                user_id="local", character_id="big_fish", conversation_id=conversation["id"],
                client_message_id=message_id, client_stream_id=stream_id, client_sequence=1,
                content=message_id,
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        same = list(pool.map(lambda _: submit(("concurrent-message-0001", "concurrent-stream-0001")), range(8)))
        different = list(pool.map(lambda n: submit((f"device-message-{n:04d}", f"device-stream-{n:04d}")), range(8)))
    assert len({item["message_id"] for item in same}) == 1
    sequences = [item["server_sequence"] for item in different]
    assert len(set(sequences)) == len(sequences)
    with db.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 9
        assert sql.execute("SELECT COUNT(DISTINCT server_sequence) FROM messages").fetchone()[0] == 9


def test_restart_recovers_saved_submission_and_authoritative_order(delivery_env):
    db, conversation, _, _ = delivery_env
    with character_scope("big_fish"):
        stored = main.message_delivery.submit(
            user_id="local", character_id="big_fish", conversation_id=conversation["id"],
            client_message_id="restart-message-0001", client_stream_id="restart-stream-0001",
            client_sequence=1, content="重启恢复",
        )
    reopened = Database(db.path)
    reopened.initialize()
    with character_scope("big_fish"):
        recovered = main.MessageDeliveryRepository(reopened).lookup(
            user_id="local", character_id="big_fish", conversation_id=conversation["id"],
            client_message_id="restart-message-0001",
        )
    assert recovered["message_id"] == stored["message_id"]
    assert recovered["server_sequence"] == stored["server_sequence"]
    assert recovered["role_status"] == "waiting"


def test_permission_failure_does_not_save(delivery_env):
    db, conversation_a, _, conversation_b = delivery_env
    response = client("fish_b").post(
        f"/api/conversations/{conversation_a['id']}/user-messages", json=payload()
    )
    assert response.status_code == 404
    with db.connection() as sql:
        assert sql.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    no_auth = TestClient(main.app, base_url="http://127.0.0.1:8001", client=("127.0.0.1", 12401))
    assert no_auth.post(
        f"/api/conversations/{conversation_b['id']}/user-messages", json=payload()
    ).status_code == 401


def test_legacy_messages_still_wait_for_model_and_return_assistant(delivery_env, monkeypatch):
    _, conversation, _, _ = delivery_env

    async def legacy(conversation_id, content):
        user = main.messages.add(conversation_id, "user", content)
        assistant = main.messages.add(conversation_id, "assistant", "旧路径回复")
        return {**assistant, "user_message_id": user["id"]}

    monkeypatch.setattr(main.chat, "send", legacy)
    response = client().post(
        f"/api/conversations/{conversation['id']}/messages", json={"content": "保持兼容"}
    )
    assert response.status_code == 200
    assert response.json()["content"] == "旧路径回复"
    with main.database.connection() as sql:
        assert sql.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 29
