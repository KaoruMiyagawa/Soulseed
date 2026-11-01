import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main as api_main
from app.db import Database
from app.memory import (
    CorrectionIntent,
    MemoryActorType,
    MemoryCorrectionService,
    MemoryCreate,
    MemoryProposalRepository,
    MemoryProposalService,
    MemoryRepository,
    MemoryService,
    MemoryStatus,
    MemoryType,
    MemoryUpdate,
    ProposalCreate,
    ProposalStatus,
    ProposalType,
)
from app.proposal_api import create_proposal_router
from app.repositories import ConversationRepository, MessageRepository


@pytest.fixture
def proposal_api_context(tmp_path):
    database = Database(str(tmp_path / "proposal-api.db"))
    database.initialize()
    conversations = ConversationRepository(database)
    messages = MessageRepository(database)
    memory_repository = MemoryRepository(database)
    memory_service = MemoryService(memory_repository)
    proposal_service = MemoryProposalService(MemoryProposalRepository(database))
    conversation = conversations.create()
    user_message = messages.add(conversation["id"], "user", "我的主要开发环境已经永久改为 Linux。真实来源证据。")
    return database, memory_repository, memory_service, proposal_service, user_message


def make_proposal(message_id, *, content="我主要使用 Linux。", target_memory_id=None, key="proposal-api"):
    return ProposalCreate(
        proposal_type=ProposalType.CORRECTION,
        target_memory_id=target_memory_id,
        proposed_memory_type=MemoryType.USER_PREFERENCE,
        proposed_content=content,
        proposed_canonical_key=f"environment:{key}",
        evidence_message_id=message_id,
        correction_intent=CorrectionIntent.EXPLICIT_CORRECTION,
        relation="conflict",
        confidence=0.9,
        evidence="结构化冲突检测说明",
        idempotency_key=key,
    )


@pytest.fixture
def client(proposal_api_context):
    database, _, _, proposal_service, _ = proposal_api_context
    correction_service = MemoryCorrectionService(database)

    def provide_service():
        return proposal_service

    api = FastAPI()
    api.include_router(create_proposal_router(provide_service, lambda: correction_service))
    with TestClient(api) as test_client:
        yield test_client


def test_list_defaults_filter_pagination_and_empty(client, proposal_api_context):
    _, _, _, service, message = proposal_api_context
    pending = service.create_proposal(make_proposal(message["id"], key="list-pending"))
    rejected = service.create_proposal(make_proposal(message["id"], content="另一个内容", key="list-rejected"))
    service.reject_proposal(rejected.id)

    default = client.get("/api/proposals")
    assert default.status_code == 200
    assert default.json()["limit"] == 50
    assert default.json()["offset"] == 0
    assert [item["id"] for item in default.json()["items"]] == [
        item.id for item in service.list_proposals(limit=50, offset=0)
    ]
    assert "idempotency_key" not in default.json()["items"][0]
    assert "request_fingerprint" not in default.json()["items"][0]

    filtered = client.get("/api/proposals", params={"status": "pending", "limit": 1, "offset": 0})
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()["items"]] == [pending.id]
    assert client.get("/api/proposals", params={"offset": 20}).json()["items"] == []


@pytest.mark.parametrize(
    "params",
    [
        {"status": "unknown"},
        {"limit": 0},
        {"limit": 1001},
        {"offset": -1},
        {"limit": "not-a-number"},
    ],
)
def test_list_rejects_invalid_query_parameters(client, params):
    assert client.get("/api/proposals", params=params).status_code == 422


def test_empty_proposal_list_is_success(client):
    response = client.get("/api/proposals")
    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 50, "offset": 0}


def test_main_app_registers_read_routes_and_uses_replaceable_service(proposal_api_context, monkeypatch):
    _, _, _, proposal_service, _ = proposal_api_context
    monkeypatch.setattr(api_main, "proposal_service", proposal_service)
    client = TestClient(api_main.app)
    try:
        response = client.get("/api/proposals")
    finally:
        client.close()
    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 50, "offset": 0}


def test_detail_returns_database_source_target_and_hides_internal_fields(client, proposal_api_context):
    _, memory_repository, memory_service, service, message = proposal_api_context
    target = memory_service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, "我主要使用 Windows。", "environment:primary"))
    proposal = service.create_proposal(make_proposal(message["id"], target_memory_id=target.id, key="detail"))

    response = client.get(f"/api/proposals/{proposal.id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["source_message"] == {
        "exists": True,
        "id": message["id"],
        "role": "user",
        "excerpt": message["content"],
        "excerpt_truncated": False,
    }
    assert payload["source_message"]["excerpt"] == message["content"]
    assert payload["proposed_content"] != payload["source_message"]["excerpt"]
    assert payload["target_memory"] == {
        "exists": True,
        "id": target.id,
        "content": "我主要使用 Windows。",
        "status": "active",
        "updated_at": target.updated_at,
    }
    assert payload["target_memory_updated_at"] == proposal.target_memory_updated_at
    assert payload["evidence"] == "结构化冲突检测说明"
    assert "idempotency_key" not in payload
    assert "request_fingerprint" not in payload
    assert "sql" not in json.dumps(payload, ensure_ascii=False).lower()


def test_detail_handles_physically_deleted_target(client, proposal_api_context):
    _, memory_repository, memory_service, service, message = proposal_api_context
    target = memory_service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, "旧环境", "environment:deleted"))
    proposal = service.create_proposal(make_proposal(message["id"], target_memory_id=target.id, key="deleted-target"))
    memory_repository.permanently_delete(target.id)

    response = client.get(f"/api/proposals/{proposal.id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["target_memory_id"] is None
    assert payload["target_memory"] == {"exists": False, "id": None, "content": None, "status": None, "updated_at": None}
    assert payload["target_memory_updated_at"] == proposal.target_memory_updated_at


def test_detail_marks_missing_source_without_failing(client, proposal_api_context):
    _, _, _, service, message = proposal_api_context
    proposal = service.create_proposal(make_proposal(message["id"], key="missing-source"))
    with service.repository.database.connection() as db:
        db.execute("DELETE FROM messages WHERE id = ?", (message["id"],))

    response = client.get(f"/api/proposals/{proposal.id}")
    assert response.status_code == 200
    assert response.json()["source_message"] == {
        "exists": False,
        "id": None,
        "role": None,
        "excerpt": None,
        "excerpt_truncated": False,
    }


def test_detail_limits_evidence_and_source_excerpt(client, proposal_api_context):
    _, _, _, service, message = proposal_api_context
    proposal_data = make_proposal(message["id"], key="long-evidence")
    proposal_data = ProposalCreate(**{**proposal_data.__dict__, "evidence": "检测说明" * 200})
    proposal = service.create_proposal(proposal_data)
    with service.repository.database.connection() as db:
        db.execute("UPDATE messages SET content = ? WHERE id = ?", ("来源文本" * 200, message["id"]))

    payload = client.get(f"/api/proposals/{proposal.id}").json()
    assert len(payload["evidence"]) == 500
    assert payload["evidence_truncated"] is True
    assert len(payload["source_message"]["excerpt"]) == 500
    assert payload["source_message"]["excerpt_truncated"] is True


def test_detail_not_found_returns_404(client):
    response = client.get("/api/proposals/missing")
    assert response.status_code == 404


def test_application_status_distinguishes_pending_accepted_and_applied(client, proposal_api_context):
    database, _, _, service, message = proposal_api_context
    pending = service.create_proposal(make_proposal(message["id"], key="status-pending"))
    accepted = service.create_proposal(make_proposal(message["id"], content="accepted only", key="status-accepted"))
    service.accept_proposal(accepted.id)
    applied = service.create_proposal(make_proposal(message["id"], content="applied proposal", key="status-applied"))
    service.accept_proposal(applied.id)
    MemoryCorrectionService(database).apply_proposal(applied.id)

    pending_payload = client.get(f"/api/proposals/{pending.id}/application").json()
    accepted_payload = client.get(f"/api/proposals/{accepted.id}/application").json()
    applied_payload = client.get(f"/api/proposals/{applied.id}/application").json()
    assert (pending_payload["status"], pending_payload["application_status"]) == ("pending", "not_applied")
    assert (accepted_payload["status"], accepted_payload["application_status"]) == ("accepted", "not_applied")
    assert (applied_payload["status"], applied_payload["application_status"]) == ("accepted", "applied")
    assert applied_payload["applied_at"]
    assert applied_payload["applied_memory_id"]


def test_application_not_found_returns_404(client):
    assert client.get("/api/proposals/missing/application").status_code == 404


def test_read_endpoints_do_not_change_proposals_memories_or_audit(proposal_api_context, client):
    database, _, memory_service, service, message = proposal_api_context
    target = memory_service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, "旧值", "readonly:target"))
    proposal = service.create_proposal(make_proposal(message["id"], target_memory_id=target.id, key="readonly"))

    def snapshot():
        with database.connection() as db:
            return {
                "proposal": tuple(db.execute("SELECT status, application_status, resolved_at, applied_at FROM memory_change_proposals WHERE id = ?", (proposal.id,)).fetchone()),
                "memory": tuple(db.execute("SELECT content, status, updated_at, last_accessed_at FROM memory_items WHERE id = ?", (target.id,)).fetchone()),
                "proposal_logs": db.execute("SELECT COUNT(*) FROM memory_proposal_change_log WHERE proposal_id = ?", (proposal.id,)).fetchone()[0],
                "application_logs": db.execute("SELECT COUNT(*) FROM memory_proposal_application_log WHERE proposal_id = ?", (proposal.id,)).fetchone()[0],
                "memory_logs": db.execute("SELECT COUNT(*) FROM memory_change_log WHERE memory_id = ?", (target.id,)).fetchone()[0],
            }

    before = snapshot()
    assert client.get("/api/proposals").status_code == 200
    assert client.get(f"/api/proposals/{proposal.id}").status_code == 200
    assert client.get(f"/api/proposals/{proposal.id}/application").status_code == 200
    assert snapshot() == before


def test_accept_and_reject_endpoints_and_idempotency(client, proposal_api_context):
    database, _, _, service, message = proposal_api_context
    accepted = service.create_proposal(make_proposal(message["id"], key="api-accept"))
    rejected = service.create_proposal(make_proposal(message["id"], content="reject case", key="api-reject"))
    with database.connection() as db:
        before_memory = db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
        before_memory_audit = db.execute("SELECT COUNT(*) FROM memory_change_log").fetchone()[0]
        before_application_log = db.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0]

    response = client.post(f"/api/proposals/{accepted.id}/accept", json={"reason": "  用户确认  ", "idempotency_key": "accept-key"})
    assert response.status_code == 200
    accepted_payload = response.json()
    assert accepted_payload == {
        "proposal_id": accepted.id,
        "status": "accepted",
        "application_status": "not_applied",
        "resolved_at": accepted_payload["resolved_at"],
        "resolution_reason": "用户确认",
    }
    assert accepted_payload["resolved_at"]
    replay = client.post(f"/api/proposals/{accepted.id}/accept", json={"reason": "用户确认", "idempotency_key": "accept-key"})
    assert replay.status_code == 200 and replay.json() == accepted_payload
    assert client.post(f"/api/proposals/{accepted.id}/accept", json={"reason": "不同原因", "idempotency_key": "accept-key"}).status_code == 409

    response = client.post(f"/api/proposals/{rejected.id}/reject", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert response.json()["application_status"] == "not_applied"
    assert response.json()["resolution_reason"] is None
    assert client.post(f"/api/proposals/{rejected.id}/reject", json={}).status_code == 200
    assert len(service.list_changes(accepted.id)) == 2
    assert len(service.list_changes(rejected.id)) == 2
    with service.repository.database.connection() as db:
        actor = db.execute(
            "SELECT actor_type FROM memory_proposal_change_log WHERE proposal_id = ? AND action = 'accepted'",
            (accepted.id,),
        ).fetchone()["actor_type"]
    assert actor == "user"
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == before_memory
        assert db.execute("SELECT COUNT(*) FROM memory_change_log").fetchone()[0] == before_memory_audit
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0] == before_application_log


@pytest.mark.parametrize(
    "path,body",
    [
        ("accept", {"reason": "   "}),
        ("reject", {"reason": "x" * 501}),
        ("accept", {"idempotency_key": " "}),
        ("reject", {"idempotency_key": "k" * 201}),
        ("accept", {"actor_type": "admin"}),
    ],
)
def test_transition_endpoints_validate_request(client, proposal_api_context, path, body):
    _, _, _, service, message = proposal_api_context
    proposal = service.create_proposal(make_proposal(message["id"], key=f"invalid-{path}-{len(str(body))}"))
    response = client.post(f"/api/proposals/{proposal.id}/{path}", json=body)
    assert response.status_code == 422


def test_transition_endpoints_map_not_found_and_state_conflict(client, proposal_api_context):
    _, _, _, service, message = proposal_api_context
    assert client.post("/api/proposals/missing/accept", json={}).status_code == 404
    accepted = service.create_proposal(make_proposal(message["id"], key="already-accepted"))
    service.accept_proposal(accepted.id)
    assert client.post(f"/api/proposals/{accepted.id}/reject", json={}).status_code == 409


def test_cancelled_and_expired_proposals_cannot_be_accepted_or_rejected(client, proposal_api_context):
    _, _, _, service, message = proposal_api_context
    cancelled = service.create_proposal(make_proposal(message["id"], key="cancelled-transition"))
    expired = service.create_proposal(make_proposal(message["id"], content="expired case", key="expired-transition"))
    service.cancel_proposal(cancelled.id)
    service.expire_proposal(expired.id)
    for proposal in (cancelled, expired):
        assert client.post(f"/api/proposals/{proposal.id}/accept", json={}).status_code == 409
        assert client.post(f"/api/proposals/{proposal.id}/reject", json={}).status_code == 409


def test_reusing_idempotency_key_for_other_operation_returns_409(client, proposal_api_context):
    _, _, _, service, message = proposal_api_context
    proposal = service.create_proposal(make_proposal(message["id"], key="conflicting-idempotency"))
    first = client.post(
        f"/api/proposals/{proposal.id}/accept",
        json={"reason": "确认", "idempotency_key": "shared-transition-key"},
    )
    assert first.status_code == 200
    conflict = client.post(
        f"/api/proposals/{proposal.id}/reject",
        json={"reason": "确认", "idempotency_key": "shared-transition-key"},
    )
    assert conflict.status_code == 409


def test_concurrent_accept_and_reject_cannot_create_conflicting_transitions(client, proposal_api_context):
    database, _, _, service, message = proposal_api_context
    enable_temp_wal(database)
    proposal = service.create_proposal(make_proposal(message["id"], key="concurrent-transition"))
    request_start = Barrier(2)

    def transition(operation):
        try:
            request_start.wait(timeout=5)
            response = client.post(f"/api/proposals/{proposal.id}/{operation}", json={})
            return (response.status_code, response.json())
        except Exception as exc:
            return ("exception", exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(transition, operation) for operation in ("accept", "reject")]
        outcomes = [future.result(timeout=8) for future in futures]

    statuses = sorted(code for code, _ in outcomes if isinstance(code, int))
    assert statuses == [200, 409]
    assert not [outcome for outcome in outcomes if outcome[0] == "exception"]
    final = service.get_proposal(proposal.id)
    assert final.status in (ProposalStatus.ACCEPTED, ProposalStatus.REJECTED)
    changes = service.list_changes(proposal.id)
    assert [change.action.value for change in changes] == ["created", final.status.value]


def test_concurrent_same_transition_replay_writes_one_terminal_audit(client, proposal_api_context):
    database, _, _, service, message = proposal_api_context
    enable_temp_wal(database)
    proposal = service.create_proposal(make_proposal(message["id"], key="concurrent-same-transition"))
    start = Barrier(2, timeout=5)

    def accept():
        start.wait()
        return client.post(
            f"/api/proposals/{proposal.id}/accept",
            json={"reason": "same", "idempotency_key": "same-transition-key"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [future.result(timeout=8) for future in (pool.submit(accept), pool.submit(accept))]
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    changes = service.list_changes(proposal.id)
    assert [change.action.value for change in changes] == ["created", "accepted"]
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log").fetchone()[0] == 0


def test_unexpected_database_error_returns_generic_500_without_details(proposal_api_context, monkeypatch):
    database, _, _, service, message = proposal_api_context
    proposal = service.create_proposal(make_proposal(message["id"], key="unexpected-db-error"))

    def fail_transition(*_args, **_kwargs):
        raise sqlite3.OperationalError("UPDATE memory_change_proposals; secret=do-not-return")

    monkeypatch.setattr(service, "accept_proposal", fail_transition)
    api = FastAPI()
    api.include_router(create_proposal_router(lambda: service, lambda: MemoryCorrectionService(database)))
    with TestClient(api, raise_server_exceptions=False) as isolated_client:
        response = isolated_client.post(f"/api/proposals/{proposal.id}/accept", json={})
    assert response.status_code == 500
    assert "secret" not in response.text
    assert "UPDATE" not in response.text


def test_apply_endpoint_atomically_applies_accepted_proposal(client, proposal_api_context):
    database, _, memory_service, service, message = proposal_api_context
    target = memory_service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, "主要使用 Windows。", "apply:windows"))
    proposal = service.create_proposal(make_proposal(message["id"], target_memory_id=target.id, key="apply-success"))
    before = client.post(f"/api/proposals/{proposal.id}/apply", json={})
    assert before.status_code == 409
    service.accept_proposal(proposal.id)

    response = client.post(f"/api/proposals/{proposal.id}/apply", json={"reason": "  确认应用  ", "idempotency_key": "apply-key"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["proposal_id"] == proposal.id
    assert payload["status"] == "accepted"
    assert payload["application_status"] == "applied"
    assert payload["applied_at"]
    assert payload["applied_memory_id"] == target.id
    assert payload["replacement_memory_id"]
    assert "request_fingerprint" not in payload
    assert "idempotency_key" not in payload
    assert memory_service.get_memory(target.id, include_inactive=True).status is MemoryStatus.SUPERSEDED
    assert memory_service.get_memory(payload["replacement_memory_id"]).content == "我主要使用 Linux。"
    with database.connection() as db:
        log = db.execute("SELECT actor_type, reason FROM memory_proposal_application_log WHERE proposal_id = ?", (proposal.id,)).fetchone()
        assert log["actor_type"] == MemoryActorType.USER.value
        assert log["reason"] == "确认应用"
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log WHERE proposal_id = ?", (proposal.id,)).fetchone()[0] == 1


@pytest.mark.parametrize("terminal", ["pending", "rejected", "cancelled", "expired"])
def test_apply_endpoint_rejects_non_accepted_proposals(client, proposal_api_context, terminal):
    _, _, _, service, message = proposal_api_context
    proposal = service.create_proposal(make_proposal(message["id"], key=f"apply-state-{terminal}"))
    if terminal != "pending":
        getattr(service, {"rejected": "reject_proposal", "cancelled": "cancel_proposal", "expired": "expire_proposal"}[terminal])(proposal.id)
    assert client.post(f"/api/proposals/{proposal.id}/apply", json={}).status_code == 409


def test_apply_endpoint_not_found_returns_404(client):
    assert client.post("/api/proposals/missing/apply", json={}).status_code == 404


def test_apply_replay_returns_existing_result_and_rejects_incompatible_retry(client, proposal_api_context):
    database, _, _, service, message = proposal_api_context
    proposal = service.create_proposal(make_proposal(message["id"], key="apply-replay"))
    service.accept_proposal(proposal.id)
    request = {"reason": "same reason", "idempotency_key": "same-apply-key"}
    first = client.post(f"/api/proposals/{proposal.id}/apply", json=request)
    replay = client.post(f"/api/proposals/{proposal.id}/apply", json=request)
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert client.post(f"/api/proposals/{proposal.id}/apply", json={**request, "reason": "different"}).status_code == 409
    assert client.post(f"/api/proposals/{proposal.id}/apply", json={"reason": "same reason", "idempotency_key": "other-key"}).status_code == 409
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log WHERE proposal_id = ?", (proposal.id,)).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("first_body", "retry_body", "expected_status"),
    [
        ({"reason": "same", "idempotency_key": "key-A"}, {"reason": "same", "idempotency_key": "key-A"}, 200),
        ({"reason": "same", "idempotency_key": "key-A"}, {"reason": "same"}, 409),
        ({"reason": "same"}, {"reason": "same", "idempotency_key": "key-A"}, 409),
        ({"reason": "same"}, {"reason": "same"}, 200),
        ({"reason": "same", "idempotency_key": "key-A"}, {"reason": "same", "idempotency_key": "key-B"}, 409),
        ({"reason": "first", "idempotency_key": "key-A"}, {"reason": "changed", "idempotency_key": "key-A"}, 409),
    ],
)
def test_apply_idempotency_key_presence_and_value_are_consistent_over_http(
    client, proposal_api_context, first_body, retry_body, expected_status
):
    database, _, memory_service, service, message = proposal_api_context
    proposal = service.create_proposal(make_proposal(message["id"], key=f"apply-key-contract:{expected_status}:{first_body}:{retry_body}"))
    service.accept_proposal(proposal.id)

    first = client.post(f"/api/proposals/{proposal.id}/apply", json=first_body)
    assert first.status_code == 200
    before_memories = memory_service.list_memories(include_inactive=True)
    before_proposal = service.get_proposal(proposal.id)
    with database.connection() as db:
        before_application_log = db.execute("SELECT * FROM memory_proposal_application_log WHERE proposal_id = ?", (proposal.id,)).fetchone()
        before_change_logs = db.execute("SELECT COUNT(*) FROM memory_change_log WHERE proposal_id = ?", (proposal.id,)).fetchone()[0]

    retry = client.post(f"/api/proposals/{proposal.id}/apply", json=retry_body)
    assert retry.status_code == expected_status
    if expected_status == 200:
        assert retry.json() == first.json()
    else:
        assert memory_service.list_memories(include_inactive=True) == before_memories
        assert service.get_proposal(proposal.id) == before_proposal
        with database.connection() as db:
            after_application_log = db.execute("SELECT * FROM memory_proposal_application_log WHERE proposal_id = ?", (proposal.id,)).fetchone()
            assert dict(after_application_log) == dict(before_application_log)
            assert db.execute("SELECT COUNT(*) FROM memory_change_log WHERE proposal_id = ?", (proposal.id,)).fetchone()[0] == before_change_logs


def test_apply_endpoint_allows_legitimate_targetless_proposal(client, proposal_api_context):
    database, _, _, service, message = proposal_api_context
    proposal = service.create_proposal(make_proposal(message["id"], key="apply-targetless"))
    service.accept_proposal(proposal.id)
    response = client.post(f"/api/proposals/{proposal.id}/apply", json={})
    assert response.status_code == 200
    assert response.json()["application_status"] == "applied"
    assert response.json()["applied_memory_id"] == response.json()["replacement_memory_id"]
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 1


def test_apply_endpoint_rejects_changed_or_deleted_target_without_partial_write(client, proposal_api_context):
    database, memory_repository, memory_service, service, message = proposal_api_context
    changed = memory_service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, "旧环境 A", "apply:changed"))
    changed_proposal = service.create_proposal(make_proposal(message["id"], target_memory_id=changed.id, key="apply-changed"))
    service.accept_proposal(changed_proposal.id)
    memory_service.update_memory(changed.id, MemoryUpdate(content="旧环境 B"))
    response = client.post(f"/api/proposals/{changed_proposal.id}/apply", json={})
    assert response.status_code == 409
    assert "目标记忆" in response.json()["detail"]
    assert service.get_proposal(changed_proposal.id).application_status.value == "not_applied"

    deleted = memory_service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, "旧环境 C", "apply:deleted"))
    deleted_proposal = service.create_proposal(make_proposal(message["id"], target_memory_id=deleted.id, key="apply-deleted"))
    service.accept_proposal(deleted_proposal.id)
    memory_repository.permanently_delete(deleted.id)
    response = client.post(f"/api/proposals/{deleted_proposal.id}/apply", json={})
    assert response.status_code == 409
    assert service.get_proposal(deleted_proposal.id).application_status.value == "not_applied"

    inactive = memory_service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, "旧环境 D", "apply:inactive"))
    inactive_proposal = service.create_proposal(make_proposal(message["id"], target_memory_id=inactive.id, key="apply-inactive"))
    service.accept_proposal(inactive_proposal.id)
    memory_service.invalidate_memory(inactive.id)
    response = client.post(f"/api/proposals/{inactive_proposal.id}/apply", json={})
    assert response.status_code == 409
    assert service.get_proposal(inactive_proposal.id).application_status.value == "not_applied"
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log WHERE proposal_id IN (?, ?, ?)", (changed_proposal.id, deleted_proposal.id, inactive_proposal.id)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_items WHERE canonical_key = 'apply:changed'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_items WHERE canonical_key = 'apply:deleted'").fetchone()[0] == 0


@pytest.mark.parametrize("source_role", ["missing", "assistant"])
def test_apply_endpoint_rejects_missing_or_non_user_evidence(client, proposal_api_context, source_role):
    database, _, _, service, message = proposal_api_context
    proposal = service.create_proposal(make_proposal(message["id"], key=f"apply-evidence-{source_role}"))
    service.accept_proposal(proposal.id)
    with database.connection() as db:
        if source_role == "missing":
            db.execute("DELETE FROM messages WHERE id = ?", (message["id"],))
        else:
            db.execute("UPDATE messages SET role = 'assistant' WHERE id = ?", (message["id"],))
    response = client.post(f"/api/proposals/{proposal.id}/apply", json={})
    assert response.status_code == 409
    assert service.get_proposal(proposal.id).application_status.value == "not_applied"
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log WHERE proposal_id = ?", (proposal.id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0


@pytest.mark.parametrize(
    "body",
    [
        {"reason": " "},
        {"reason": "x" * 501},
        {"idempotency_key": " "},
        {"idempotency_key": "k" * 201},
        {"actor_type": "admin"},
        {"target_memory_id": "forced-target"},
        {"proposed_content": "forced content"},
    ],
)
def test_apply_endpoint_validates_body_and_forbids_overrides(client, proposal_api_context, body):
    _, _, _, service, message = proposal_api_context
    proposal = service.create_proposal(make_proposal(message["id"], key=f"apply-invalid-{len(str(body))}"))
    assert client.post(f"/api/proposals/{proposal.id}/apply", json=body).status_code == 422


def test_apply_failure_rolls_back_all_memory_and_audit_writes(client, proposal_api_context, monkeypatch):
    database, _, memory_service, service, message = proposal_api_context
    proposal = service.create_proposal(make_proposal(message["id"], key="apply-rollback"))
    service.accept_proposal(proposal.id)

    def fail_source_insert(*_args, **_kwargs):
        raise RuntimeError("injected source write failure")

    monkeypatch.setattr(MemoryCorrectionService, "_insert_source", staticmethod(fail_source_insert))
    with pytest.raises(RuntimeError, match="injected source write failure"):
        client.post(f"/api/proposals/{proposal.id}/apply", json={})
    assert memory_service.list_memories() == []
    assert service.get_proposal(proposal.id).application_status.value == "not_applied"
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log WHERE proposal_id = ?", (proposal.id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM memory_change_log WHERE proposal_id = ?", (proposal.id,)).fetchone()[0] == 0


def test_unexpected_apply_error_returns_generic_500(proposal_api_context, monkeypatch):
    database, _, _, service, message = proposal_api_context
    proposal = service.create_proposal(make_proposal(message["id"], key="apply-unexpected-error"))
    service.accept_proposal(proposal.id)
    correction_service = MemoryCorrectionService(database)

    def fail_apply(*_args, **_kwargs):
        raise sqlite3.OperationalError("SQL SECRET=not-for-client")

    monkeypatch.setattr(correction_service, "apply_proposal", fail_apply)
    api = FastAPI()
    api.include_router(create_proposal_router(lambda: service, lambda: correction_service))
    with TestClient(api, raise_server_exceptions=False) as isolated_client:
        response = isolated_client.post(f"/api/proposals/{proposal.id}/apply", json={})
    assert response.status_code == 500
    assert "SECRET" not in response.text
    assert "SQL" not in response.text


def test_apply_endpoint_concurrent_replay_has_one_application_effect(client, proposal_api_context):
    database, _, _, service, message = proposal_api_context
    enable_temp_wal(database)
    proposal = service.create_proposal(make_proposal(message["id"], key="apply-concurrent"))
    service.accept_proposal(proposal.id)
    start = Barrier(2, timeout=5)

    def apply():
        start.wait()
        return client.post(f"/api/proposals/{proposal.id}/apply", json={"reason": "same", "idempotency_key": "concurrent-apply"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [future.result(timeout=8) for future in (pool.submit(apply), pool.submit(apply))]
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_proposal_application_log WHERE proposal_id = ?", (proposal.id,)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 1


def enable_temp_wal(database):
    with database.connection() as db:
        mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    assert mode.lower() == "wal"


def pause_after_proposal_snapshot(repository, monkeypatch, writer_finished):
    read_started = Event()
    original = repository._proposal_row
    paused = False

    def read_then_pause(db, proposal_id):
        nonlocal paused
        row = original(db, proposal_id)
        if not paused:
            paused = True
            assert db.in_transaction
            read_started.set()
            if not writer_finished.wait(timeout=5):
                raise TimeoutError("并发写入未在限定时间内完成")
        return row

    monkeypatch.setattr(repository, "_proposal_row", read_then_pause)
    return read_started


def test_detail_snapshot_survives_concurrent_target_deletion(proposal_api_context, monkeypatch):
    database, memory_repository, memory_service, service, message = proposal_api_context
    enable_temp_wal(database)
    target = memory_service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE, "删除前目标", "snapshot:delete"))
    proposal = service.create_proposal(make_proposal(message["id"], target_memory_id=target.id, key="snapshot-delete"))
    read_repository = service.repository
    writer_finished = Event()
    read_started = pause_after_proposal_snapshot(read_repository, monkeypatch, writer_finished)

    def read_details():
        return read_repository.get_with_related_read_data(proposal.id)

    def delete_target():
        try:
            memory_repository.permanently_delete(target.id)
        finally:
            writer_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(read_details)
        try:
            assert read_started.wait(timeout=5), "读取线程未建立 Proposal 快照"
            writer = pool.submit(delete_target)
            writer.result(timeout=5)
        finally:
            writer_finished.set()
        details = reader.result(timeout=5)

    read_proposal, read_target, read_source = details
    assert read_proposal.target_memory_id == target.id
    assert read_target == {
        "id": target.id,
        "content": "删除前目标",
        "status": "active",
        "updated_at": target.updated_at,
    }
    assert read_source["id"] == message["id"]
    current, current_target, _ = read_repository.get_with_related_read_data(proposal.id)
    assert current.target_memory_id is None
    assert current_target is None


def test_detail_snapshot_survives_concurrent_proposal_accept(proposal_api_context, monkeypatch):
    database, _, _, service, message = proposal_api_context
    enable_temp_wal(database)
    proposal = service.create_proposal(make_proposal(message["id"], key="snapshot-accept"))
    repository = service.repository
    writer_finished = Event()
    read_started = pause_after_proposal_snapshot(repository, monkeypatch, writer_finished)

    def read_details():
        return repository.get_with_related_read_data(proposal.id)

    def accept_proposal():
        try:
            return service.accept_proposal(proposal.id)
        finally:
            writer_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(read_details)
        try:
            assert read_started.wait(timeout=5), "读取线程未建立 Proposal 快照"
            writer = pool.submit(accept_proposal)
            assert writer.result(timeout=5).status is ProposalStatus.ACCEPTED
        finally:
            writer_finished.set()
        old_snapshot, _, _ = reader.result(timeout=5)

    assert old_snapshot.status is ProposalStatus.PENDING
    assert repository.get(proposal.id).status is ProposalStatus.ACCEPTED


def test_read_transaction_is_ended_on_success_and_exception(proposal_api_context, monkeypatch):
    database, _, _, service, message = proposal_api_context
    proposal = service.create_proposal(make_proposal(message["id"], key="transaction-cleanup"))
    repository = service.repository

    original_connection = database.connection
    observed = []

    from contextlib import contextmanager

    @contextmanager
    def observing_connection():
        with original_connection() as db:
            observed.append(db)
            yield db

    monkeypatch.setattr(database, "connection", observing_connection)
    repository.get_with_related_read_data(proposal.id)
    with pytest.raises(sqlite3.ProgrammingError):
        observed[-1].execute("SELECT 1")

    original_row = repository._proposal_row
    failed_connections = []

    def fail_after_begin(db, proposal_id):
        failed_connections.append(db)
        assert db.in_transaction
        original_row(db, proposal_id)
        raise RuntimeError("injected read failure")

    monkeypatch.setattr(repository, "_proposal_row", fail_after_begin)
    with pytest.raises(RuntimeError, match="injected read failure"):
        repository.get_with_related_read_data(proposal.id)
    with pytest.raises(sqlite3.ProgrammingError):
        failed_connections[0].execute("SELECT 1")

    monkeypatch.setattr(repository, "_proposal_row", original_row)
    assert repository.get_with_related_read_data(proposal.id)[0].id == proposal.id
