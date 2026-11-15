"""Durable reply execution over C1 submissions. Transactions never span model awaits."""

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException

from .chat_attempts import ChatAttempt, chat_attempt_factory
from .chat_service import ChatService, ReplyExecution, ReplyExecutionError
from .db import Database
from .desktop_runtime import DesktopBudget, DesktopDatabase
from .model_adapter import ModelConfigurationError
from .ownership import character_scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplyClaim:
    user_message: dict
    character_id: str
    token: str

    @property
    def message_id(self) -> str:
        return self.user_message["id"]


class ConversationReplyBusy(RuntimeError):
    pass


class ReplyQueue:
    def __init__(self, database: Database, *, clock=lambda: datetime.now(UTC),
                 lease: timedelta = timedelta(minutes=2), conversation_id: str | None = None,
                 max_messages: int = 100, max_attempts: int = 3, max_output_tokens: int = 512):
        self.database, self.clock, self.lease = database, clock, lease
        self.conversation_id = conversation_id
        self.max_messages, self.max_attempts, self.max_output_tokens = max_messages, max_attempts, max_output_tokens

    @staticmethod
    def _expire(db, now: str) -> None:
        db.execute(
            """UPDATE chat_model_attempts
               SET outcome=CASE WHEN outcome='sent' THEN 'unknown' ELSE 'local_failure' END,
                   ended_at=?,error_code='execution_lease_expired'
               WHERE outcome IN ('prepared','sent') AND user_message_id IN (
                   SELECT message_id FROM client_message_submissions
                   WHERE role_status='processing' AND reply_lease_until<=?)""", (now, now),
        )
        db.execute(
            """UPDATE client_message_submissions
               SET role_status=CASE WHEN reply_started_at IS NULL THEN 'waiting' ELSE 'unknown' END,
                   reply_error_code=CASE WHEN reply_started_at IS NULL THEN NULL ELSE 'execution_lease_expired' END,
                   reply_lease_token=NULL,reply_lease_until=NULL
               WHERE role_status='processing' AND reply_lease_until<=?""", (now,),
        )
        db.execute("DELETE FROM chat_legacy_turns WHERE expires_at<=?", (now,))

    def claim(self) -> ReplyClaim | None:
        with self.database.atomic() as db:
            now = self.clock()
            self._expire(db, now.isoformat())
            row = db.execute(
                """SELECT m.id,m.conversation_id,m.role,m.content,m.created_at,m.server_sequence,s.character_id
                   FROM client_message_submissions s
                   JOIN messages m ON m.id=s.message_id AND m.conversation_id=s.conversation_id AND m.role='user'
                   JOIN conversations c ON c.id=s.conversation_id AND c.character_id=s.character_id
                   JOIN characters ch ON ch.id=c.character_id AND ch.owner_user_id=s.user_id AND ch.ownership_status='owned'
                   WHERE s.role_status='waiting' AND (? IS NULL OR c.id=?)
                     AND NOT EXISTS (SELECT 1 FROM chat_legacy_turns l WHERE l.conversation_id=c.id)
                     AND NOT EXISTS (
                         SELECT 1 FROM client_message_submissions p JOIN messages prior ON prior.id=p.message_id
                         WHERE p.conversation_id=c.id AND p.role_status IN ('waiting','processing','unknown')
                           AND prior.server_sequence<m.server_sequence
                     )
                   ORDER BY m.created_at,m.conversation_id,m.server_sequence LIMIT 1""",
                (self.conversation_id, self.conversation_id),
            ).fetchone()
            if row is None:
                return None
            token = str(uuid4())
            db.execute(
                """UPDATE client_message_submissions SET role_status='processing',reply_lease_token=?,reply_lease_until=?
                   WHERE message_id=?""",
                (token, (now + self.lease).isoformat(), row["id"]),
            )
            message = dict(row)
            return ReplyClaim(message, message.pop("character_id"), token)

    def _live(self, db, claim: ReplyClaim):
        row = db.execute(
            """SELECT s.reply_started_at,s.model_started_at FROM client_message_submissions s
               JOIN conversations c ON c.id=s.conversation_id AND c.character_id=s.character_id
               JOIN characters ch ON ch.id=c.character_id AND ch.owner_user_id=s.user_id AND ch.ownership_status='owned'
               WHERE s.message_id=? AND s.character_id=current_character() AND s.role_status='processing'
                 AND s.reply_lease_token=? AND s.reply_lease_until>?""",
            (claim.message_id, claim.token, self.clock().isoformat()),
        ).fetchone()
        if row is None:
            raise ReplyExecutionError("角色回复租约已失效")
        return row

    def begin(self, claim: ReplyClaim) -> None:
        with self.database.atomic() as db:
            if self._live(db, claim)["reply_started_at"] is not None:
                raise ReplyExecutionError("角色回复已经开始执行")
            db.execute("UPDATE client_message_submissions SET reply_started_at=? WHERE message_id=?",
                       (self.clock().isoformat(), claim.message_id))

    def start_attempt(self, claim: ReplyClaim, phase: str, configured_max: int | None) -> ChatAttempt:
        with self.database.atomic() as db:
            if self._live(db, claim)["reply_started_at"] is None:
                raise ReplyExecutionError("角色回复尚未开始执行")
            ordinal = db.execute(
                "SELECT COUNT(*)+1 FROM chat_model_attempts WHERE user_message_id=?", (claim.message_id,),
            ).fetchone()[0]
            if ordinal > self.max_attempts:
                raise ReplyExecutionError("本轮模型调用次数已达上限", code="reply_attempt_limit")
            attempt_id = f"{claim.message_id}:{ordinal}"
            cap = min(configured_max or self.max_output_tokens, self.max_output_tokens)
            db.execute(
                """INSERT INTO chat_model_attempts(attempt_id,user_message_id,ordinal,phase,started_at,outcome,max_output_tokens)
                   VALUES (?,?,?,?,?,'prepared',?)""",
                (attempt_id, claim.message_id, ordinal, phase, self.clock().isoformat(), cap),
            )

        def sent() -> None:
            with self.database.atomic() as db:
                self._live(db, claim)
                if not db.execute(
                    "UPDATE chat_model_attempts SET outcome='sent',sent_at=? WHERE attempt_id=? AND outcome='prepared'",
                    (self.clock().isoformat(), attempt_id),
                ).rowcount:
                    raise ReplyExecutionError("模型尝试已经跨过发送边界", code="reply_attempt_already_sent")

        def responded(status: int, provider_id: str | None) -> None:
            with self.database.atomic() as db:
                self._live(db, claim)
                if not db.execute(
                    """UPDATE chat_model_attempts SET outcome='response',ended_at=?,http_status=?,provider_request_id=?
                       WHERE attempt_id=? AND outcome='sent'""",
                    (self.clock().isoformat(), status, provider_id, attempt_id),
                ).rowcount:
                    raise ReplyExecutionError("模型尝试尚未跨过发送边界", code="reply_attempt_not_sent")

        def interrupted(reason: str) -> None:
            with self.database.atomic() as db:
                self._expire(db, self.clock().isoformat())
                db.execute(
                    """UPDATE chat_model_attempts
                       SET outcome=CASE WHEN outcome='sent' THEN 'unknown' ELSE 'local_failure' END,
                           ended_at=?,error_code=? WHERE attempt_id=? AND outcome IN ('prepared','sent')""",
                    (self.clock().isoformat(), reason, attempt_id),
                )

        return ChatAttempt(attempt_id, claim.character_id, cap, sent, responded, interrupted)

    def execution(self, claim: ReplyClaim) -> ReplyExecution:
        def check() -> None:
            with self.database.connection() as db:
                self._live(db, claim)

        def model_start() -> None:
            with self.database.atomic() as db:
                if self._live(db, claim)["reply_started_at"] is None:
                    raise ReplyExecutionError("角色回复尚未开始执行")
                db.execute(
                    """UPDATE client_message_submissions SET model_started_at=COALESCE(model_started_at,?)
                       WHERE message_id=?""", (self.clock().isoformat(), claim.message_id),
                )

        def confirm(assistant: dict) -> None:
            if not self.database.in_transaction:
                raise ReplyExecutionError("回复确认需要与消息使用同一事务")
            with self.database.connection() as db:
                self._live(db, claim)
                if db.execute("SELECT user_message_id FROM messages WHERE id=?",
                              (assistant["id"],)).fetchone()[0] != claim.message_id:
                    raise ReplyExecutionError("回复来源与认领记录不一致", code="reply_commit_failed")
                db.execute(
                    """UPDATE chat_model_attempts SET reply_message_id=? WHERE attempt_id=(
                       SELECT attempt_id FROM chat_model_attempts WHERE user_message_id=?
                           AND outcome='response' AND http_status BETWEEN 200 AND 299 ORDER BY ordinal DESC LIMIT 1)""",
                    (assistant["id"], claim.message_id),
                )
                self._finish(db, claim.message_id, "completed", None)

        return ReplyExecution(check, model_start, confirm, stop_on_model_error=True)

    @staticmethod
    def _finish(db, message_id: str, status: str, error: str | None) -> None:
        db.execute(
            """UPDATE client_message_submissions SET role_status=?,reply_error_code=?,
                   reply_lease_token=NULL,reply_lease_until=NULL WHERE message_id=?""",
            (status, error, message_id),
        )

    def interrupted(self, claim: ReplyClaim, reason: str, *, force_unknown: bool = False) -> None:
        with self.database.atomic() as db:
            self._expire(db, self.clock().isoformat())
            try:
                row = self._live(db, claim)
            except ReplyExecutionError:
                return
            attempts = db.execute(
                "SELECT outcome,http_status FROM chat_model_attempts WHERE user_message_id=?", (claim.message_id,),
            ).fetchall()
            if force_unknown:
                db.execute(
                    """UPDATE chat_model_attempts SET outcome=CASE WHEN outcome='sent' THEN 'unknown' ELSE 'local_failure' END,
                       ended_at=?,error_code=? WHERE user_message_id=? AND outcome IN ('prepared','sent')""",
                    (self.clock().isoformat(), reason, claim.message_id),
                )
            known_failure = bool(attempts) and all(
                item["outcome"] == "local_failure" or
                (item["outcome"] == "response" and 400 <= item["http_status"] < 500 and item["http_status"] != 408)
                for item in attempts
            )
            if known_failure and db.execute(
                """SELECT 1 FROM memory_correction_clarifications WHERE character_id=current_character()
                   AND (trigger_message_id=? OR response_message_id=?) AND status IN ('preparing','processing','failed')
                   LIMIT 1""", (claim.message_id, claim.message_id),
            ).fetchone():
                known_failure = False
                reason = "clarification_review_required"
            if known_failure and reason == "model_outcome_unknown":
                reason = "model_http_failure" if any(item["outcome"] == "response" for item in attempts) else "model_local_failure"
            status = "unknown" if force_unknown or (row["model_started_at"] is not None and not known_failure) else "failed"
            self._finish(db, claim.message_id, status, reason)

    @staticmethod
    def _check_async_pending(db, conversation_id: str) -> None:
        if db.execute(
            """SELECT 1 FROM client_message_submissions WHERE conversation_id=?
               AND role_status IN ('waiting','processing','unknown') LIMIT 1""", (conversation_id,),
        ).fetchone():
            raise ConversationReplyBusy("当前会话有待处理或待核对的回复")

    @contextmanager
    def legacy_turn(self, conversation_id: str):
        token = str(uuid4())
        with self.database.atomic() as db:
            now = self.clock()
            self._expire(db, now.isoformat())
            self._check_async_pending(db, conversation_id)
            if db.execute("SELECT 1 FROM chat_legacy_turns WHERE conversation_id=? LIMIT 1",
                          (conversation_id,)).fetchone():
                raise ConversationReplyBusy("当前会话有待处理或待核对的回复")
            db.execute(
                "INSERT INTO chat_legacy_turns(conversation_id,token,expires_at) VALUES (?,?,?)",
                (conversation_id, token, (now + timedelta(minutes=20)).isoformat()),
            )

        def check() -> None:
            with self.database.connection() as db:
                row = db.execute(
                    """SELECT 1 FROM chat_legacy_turns WHERE token=? AND expires_at>?""",
                    (token, self.clock().isoformat()),
                ).fetchone()
            if row is None:
                raise ReplyExecutionError("同步聊天执行权已失效")

        def before_user_save() -> None:
            # The caller holds the same write transaction through the user insert.
            check()
            with self.database.connection() as db:
                self._check_async_pending(db, conversation_id)

        try:
            yield ReplyExecution(check, check, lambda _: None, before_user_save=before_user_save)
        finally:
            with self.database.atomic() as db:
                db.execute("DELETE FROM chat_legacy_turns WHERE token=?", (token,))


class ChatReplyWorker:
    def __init__(self, queue: ReplyQueue, chat: ChatService, *, timeout: float = 90):
        model = getattr(chat, "model", None)
        budget_service = getattr(model, "budget_service", None) if getattr(model, "strict_budget", False) else None
        desktop_budget = (type(queue.database) is DesktopDatabase
                          and type(budget_service) is DesktopBudget
                          and budget_service.database is queue.database)
        if queue.database.environment not in ("testing", "development") and not desktop_budget:
            raise ModelConfigurationError("strict_budget_required_for_live_worker")
        if queue.database.environment == "development" and budget_service is None:
            raise ModelConfigurationError("development_strict_budget_required")
        if chat.messages.database is not queue.database:
            raise ValueError("回复 Worker 与 ChatService 必须共享 Database 实例")
        if not 0 < timeout < queue.lease.total_seconds():
            raise ValueError("回复处理超时必须短于租约")
        self.queue, self.chat, self.timeout = queue, chat, timeout
        self.budget_service = budget_service
        self.stopping = asyncio.Event()
        self.tasks: list[asyncio.Task] = []
        self.active: dict[str, ReplyClaim] = {}

    @property
    def accepting(self) -> bool:
        return not self.stopping.is_set() and any(not task.done() for task in self.tasks)

    def start(self, *, concurrency: int = 1) -> None:
        if self.tasks or self.stopping.is_set() or concurrency not in (1, 2):
            raise ValueError("Worker 启动状态或并发数无效")
        if self.queue.conversation_id is not None and concurrency != 1:
            raise ValueError("单会话试运行只允许一个 Worker")
        self.tasks = [asyncio.create_task(self.run()) for _ in range(concurrency)]

    def admit(self, db, conversation_id: str) -> None:
        if (not self.accepting or self.queue.conversation_id not in (None, conversation_id)):
            raise HTTPException(503, {"code": "async_chat_unavailable", "message": "异步回复当前不可用，消息未保存"})
        if (self.queue.database.environment == "development" and
                (self.budget_service is None or not self.budget_service.ready(require_development_cutover=True))):
            raise HTTPException(503, {"code": "async_chat_budget_unavailable",
                                      "message": "统一模型预算尚未完成历史切换，消息未保存"})
        count = db.execute("SELECT COUNT(*) FROM client_message_submissions WHERE conversation_id=?",
                           (conversation_id,)).fetchone()[0]
        if count >= self.queue.max_messages:
            raise HTTPException(409, {"code": "async_chat_task_limit", "message": "会话已达到异步任务上限"})

    async def drain(self, grace: float = 10) -> None:
        # Same write boundary as admission. All later admissions observe stopping.
        with self.queue.database.atomic():
            self.stopping.set()
        if not self.tasks:
            return
        _, pending = await asyncio.wait(self.tasks, timeout=grace)
        for task in pending:
            task.cancel()
        if pending:
            _, pending = await asyncio.wait(pending, timeout=1)
        if pending:
            # Fence an adapter that does not cooperate with cancellation.
            for claim in list(self.active.values()):
                with character_scope(claim.character_id):
                    self.queue.interrupted(claim, "worker_drain_expired", force_unknown=True)
            logger.error("chat_reply_drain_expired unfinished=%s", len(pending))

    async def run_one(self) -> bool:
        if self.stopping.is_set():
            return False
        claim = self.queue.claim()
        if claim is None:
            return False
        with character_scope(claim.character_id):
            try:
                self.queue.begin(claim)
            except ReplyExecutionError:
                return True
            self.active[claim.token] = claim
            token = chat_attempt_factory.set(
                lambda phase, maximum: self.queue.start_attempt(claim, phase, maximum)
            )
            try:
                async with asyncio.timeout(self.timeout):
                    await self.chat.reply_to_saved(claim.user_message, self.queue.execution(claim))
            except asyncio.CancelledError:
                self.queue.interrupted(claim, "worker_cancelled")
                raise
            except Exception as exc:
                if isinstance(exc, ReplyExecutionError):
                    reason = exc.code
                else:
                    reason = "reply_timeout" if isinstance(exc, TimeoutError) else "reply_execution_failed"
                self.queue.interrupted(claim, reason)
                logger.warning("chat_reply_interrupted message_id=%s error_type=%s", claim.message_id, type(exc).__name__)
            finally:
                chat_attempt_factory.reset(token)
                self.active.pop(claim.token, None)
        return True

    async def run(self, *, poll_interval: float = 0.25) -> None:
        while not self.stopping.is_set():
            try:
                if await self.run_one():
                    await asyncio.sleep(0)
                    continue
            except Exception as exc:
                logger.error("chat_reply_worker_error error_type=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(self.stopping.wait(), poll_interval)
            except TimeoutError:
                pass


class DelayedMockChatModel:
    def __init__(self, delay: float = 0.25):
        self.delay = delay

    async def reply(self, messages: list[dict[str, str]]) -> str:
        await asyncio.sleep(self.delay)
        return "我收到你的消息了。"
