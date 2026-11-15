"""Owner-scoped diagnostics and evidence-bound review of uncertain replies."""

from hashlib import sha256
import json

from fastapi import HTTPException

from .db import Database
from .repositories import now_iso


class ChatReplyReview:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _snapshot(db, message_id: str, user_id: str) -> dict:
        task = db.execute(
            """SELECT s.message_id,s.conversation_id,s.role_status,s.reply_error_code,
                      s.reply_started_at,s.model_started_at,s.reply_lease_until
               FROM client_message_submissions s JOIN conversations c ON c.id=s.conversation_id
               JOIN characters ch ON ch.id=c.character_id AND ch.id=s.character_id
               WHERE s.message_id=? AND s.user_id=? AND ch.owner_user_id=s.user_id
                 AND ch.id=current_character() AND ch.ownership_status='owned'""",
            (message_id, user_id),
        ).fetchone()
        if task is None:
            raise HTTPException(404, "异步消息不存在")

        def rows(query, *args):
            return [dict(row) for row in db.execute(query, args)]

        reply = db.execute(
            "SELECT id,server_sequence,created_at FROM messages WHERE user_message_id=? AND role='assistant'",
            (message_id,),
        ).fetchone()
        blocker = db.execute(
            """SELECT s.message_id,s.role_status,s.reply_error_code
               FROM client_message_submissions s JOIN messages m ON m.id=s.message_id
               JOIN messages current ON current.id=? AND current.conversation_id=m.conversation_id
               WHERE s.conversation_id=? AND m.server_sequence<current.server_sequence
                 AND s.role_status IN ('waiting','processing','unknown')
               ORDER BY m.server_sequence LIMIT 1""", (message_id, task["conversation_id"]),
        ).fetchone() if task["role_status"] == "waiting" else None
        evidence = {
            "task": dict(task),
            "blocked_by": dict(blocker) if blocker else None,
            "saved_reply": dict(reply) if reply else None,
            "attempts": rows("SELECT * FROM chat_model_attempts WHERE user_message_id=? ORDER BY ordinal", message_id),
            "memory_sources": rows(
                """SELECT id,memory_id,evidence_type,created_at FROM memory_sources
                   WHERE character_id=current_character() AND message_id=? ORDER BY id""", message_id,
            ),
            "memory_changes": rows(
                """SELECT id,memory_id,action,proposal_id,created_at FROM memory_change_log
                   WHERE character_id=current_character() AND source_message_id=? ORDER BY id""", message_id,
            ),
            "proposals": rows(
                """SELECT p.id,p.status,p.application_status,a.id AS application_log_id
                   FROM memory_change_proposals p LEFT JOIN memory_proposal_application_log a
                     ON a.proposal_id=p.id AND a.character_id=p.character_id
                   WHERE p.character_id=current_character() AND p.evidence_message_id=? ORDER BY p.id""",
                message_id,
            ),
            "clarifications": rows(
                """SELECT id,status,version,trigger_message_id,response_message_id,assistant_message_id,
                          source_proposal_id,application_proposal_id,lease_expires_at,error_code
                   FROM memory_correction_clarifications WHERE character_id=current_character()
                       AND (trigger_message_id=? OR response_message_id=?) ORDER BY id""",
                message_id, message_id,
            ),
        }
        review = db.execute("SELECT * FROM chat_reply_reviews WHERE user_message_id=?", (message_id,)).fetchone()
        return {
            **evidence,
            "snapshot_token": sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(),
            "review": dict(review) if review else None,
        }

    def inspect(self, message_id: str, user_id: str) -> dict:
        with self.database.connection() as db:
            db.execute("BEGIN")
            return self._snapshot(db, message_id, user_id)

    def resolve(self, message_id: str, user_id: str, *, snapshot_token: str, decision: str,
                evidence_reference: str, provider_checked: bool) -> dict:
        evidence_reference = evidence_reference.strip()
        if len(evidence_reference) < 8:
            raise HTTPException(422, "需要可核验的证据引用")
        with self.database.atomic() as db:
            record = self._snapshot(db, message_id, user_id)
            review = record["review"]
            if review and (review["snapshot_token"], review["decision"], review["evidence_reference"],
                           review["provider_checked"]) == (
                snapshot_token, decision, evidence_reference, provider_checked,
            ):
                return record
            if record["task"]["role_status"] != "unknown" or record["task"]["reply_lease_until"]:
                raise HTTPException(409, "只有无有效租约的 unknown 任务可人工处置")
            if record["snapshot_token"] != snapshot_token:
                raise HTTPException(409, "核对快照已变化，请重新查询")
            attempts = record["attempts"]
            if any(item["outcome"] in ("prepared", "sent") for item in attempts):
                raise HTTPException(409, "模型尝试尚未结算")
            if (not attempts or any(item["outcome"] == "unknown" for item in attempts)) and not provider_checked:
                raise HTTPException(409, "需先核对可能已发送的模型请求并确认外部证据")
            if decision not in ("confirm_saved_reply", "close_without_reply"):
                raise HTTPException(422, "处置结论无效")
            saved = record["saved_reply"] is not None
            if saved != (decision == "confirm_saved_reply"):
                raise HTTPException(409, "处置结论与实际保存的回复不一致")

            now = now_iso()
            proposals = {item["id"]: item for item in record["proposals"]}
            for item in record["clarifications"]:
                if item["lease_expires_at"] and item["lease_expires_at"] > now:
                    raise HTTPException(409, "关联澄清仍持有租约")
                if item["status"] not in ("preparing", "processing", "failed"):
                    continue
                proposal_id = item["application_proposal_id"]
                proposal = proposals.get(proposal_id)
                if proposal_id and (not proposal or not proposal["application_log_id"]
                                    and proposal["status"] not in ("rejected", "cancelled", "expired")):
                    raise HTTPException(409, "关联提案仍未核对，不能解除阻塞")
                status = "applied" if proposal and proposal["application_log_id"] else "deferred"
                db.execute(
                    """UPDATE memory_correction_clarifications SET status=?,version=version+1,
                       processing_token=NULL,lease_expires_at=NULL,error_code=?,updated_at=?
                       WHERE id=? AND character_id=current_character()""",
                    (status, None if status == "applied" else "reply_manually_closed", now, item["id"]),
                )
            db.execute(
                """INSERT INTO chat_reply_reviews(user_message_id,reviewer_user_id,decision,snapshot_token,
                   evidence_reference,provider_checked,reviewed_at) VALUES(?,?,?,?,?,?,?)""",
                (message_id, user_id, decision, snapshot_token, evidence_reference, provider_checked, now),
            )
            db.execute(
                "UPDATE client_message_submissions SET role_status=?,reply_error_code=? WHERE message_id=?",
                ("completed" if saved else "failed", None if saved else "reply_manually_closed", message_id),
            )
            return self._snapshot(db, message_id, user_id)
