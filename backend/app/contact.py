"""Durable, bounded decisions for in-app proactive contact."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Callable, Protocol
from uuid import uuid4

from .life import LifeRepository, _iso

UNSET = object()


class ContactGenerator(Protocol):
    async def generate(self, intent: dict, *, reminder: bool = False) -> str: ...


class SafeContactGenerator:
    """Local fallback built only from saved intent fields; it never calls a model."""

    async def generate(self, intent: dict, *, reminder: bool = False) -> str:
        topic = (intent.get("topic") or "").strip()
        if not topic:
            raise ValueError("联系意图缺少可表达的内容")
        if reminder:
            return f"之前提到的「{topic}」还在。等你方便时再看就好。"
        if intent["source"] == "life_share":
            raise ValueError("探索发现分享需要显式启用角色表达生成")
        if intent["source"] == "reminder":
            return f"想轻轻提醒你一下「{topic}」。等你方便时再看就好。"
        return f"之前你提到「{topic}」，我还想继续听听。你有空再说也好。"


class AdapterContactGenerator:
    """Opt-in generator.  Its only life facts come from a character-scoped DB lookup."""

    def __init__(self, model, persona_prompt: str, repository: LifeRepository | None = None):
        self.model, self.persona_prompt, self.repository = model, persona_prompt, repository

    async def generate(self, intent: dict, *, reminder: bool = False) -> str:
        from .expression import ExpressionContextBuilder

        character_id = str(intent.get("character_id") or "")
        if not character_id:
            raise ValueError("主动联系意图缺少角色范围")
        evidence, recent, prefs = [], [], []
        if self.repository is not None:
            snapshot = self.repository.expression_snapshot("最近在干嘛", character_id=character_id)
            evidence = snapshot["events"]
            prefs = snapshot["preferences"]
            with self.repository.database.connection() as db:
                if intent.get("source") == "life_share":
                    row = db.execute(
                        "SELECT e.id AS event_id, e.event_type, e.content, e.occurred_at, e.status, e.source, e.generation_method, s.reason AS share_reason FROM life_share_intents s JOIN life_events e ON e.id=s.event_id AND e.character_id=s.character_id WHERE s.id=? AND s.character_id=? AND s.status='pending' AND e.activity_status='completed' AND e.status='completed'",
                        (intent.get("share_intent_id"), character_id),
                    ).fetchone()
                    if not row:
                        raise ValueError("分享来源不再是已完成的角色经历")
                    share = dict(row)
                    if share["event_type"] == "research":
                        run = db.execute(
                            "SELECT id,summary,claims_json,unanswered_json FROM exploration_runs "
                            "WHERE life_event_id=? AND character_id=? AND status='completed'",
                            (share["event_id"], character_id),
                        ).fetchone()
                        if not run:
                            raise ValueError("探索发现缺少已完成的研究记录")
                        try:
                            claims = json.loads(run["claims_json"] or "[]")
                            unanswered = json.loads(run["unanswered_json"] or "[]")
                        except (TypeError, json.JSONDecodeError) as exc:
                            raise ValueError("探索发现的保存结论无法读取") from exc
                        source_ids = list(dict.fromkeys(
                            source_id for claim in claims if isinstance(claim, dict)
                            and claim.get("kind") == "sourced"
                            and isinstance(claim.get("source_ids"), list)
                            for source_id in claim["source_ids"] if isinstance(source_id, str)
                        ))
                        sources = []
                        if source_ids:
                            marks = ",".join("?" for _ in source_ids)
                            sources = [dict(item) for item in db.execute(
                                f"SELECT id,source_url,title,retrieved_at,excerpt FROM exploration_evidence "
                                f"WHERE run_id=? AND character_id=? AND id IN ({marks}) "
                                "AND tool_name='read_webpage' AND outcome='success' "
                                "AND page_relevance='relevant' AND excerpt IS NOT NULL ORDER BY sequence_no LIMIT 4",
                                (run["id"], character_id, *source_ids),
                            ).fetchall()]
                        if not any(isinstance(claim, dict) and claim.get("kind") == "sourced"
                                   and any(source["id"] in claim.get("source_ids", []) for source in sources)
                                   for claim in claims):
                            raise ValueError("探索发现没有有效来源支持")
                        share["research_result"] = {
                            "summary": run["summary"],
                            "claims": claims[:8],
                            "unanswered": unanswered[:8],
                            "sources": [{
                                "evidence_id": item["id"], "url": item["source_url"],
                                "title": item["title"], "retrieved_at": item["retrieved_at"],
                                "excerpt": item["excerpt"][:500] if item["excerpt"] else None,
                            } for item in sources],
                        }
                        sourced_count = sum(1 for claim in claims if isinstance(claim, dict)
                                            and claim.get("kind") == "sourced"
                                            and any(source["id"] in claim.get("source_ids", []) for source in sources))
                        if sourced_count < 2 and len(str(run["summary"] or "").strip()) < 90:
                            raise ValueError("探索发现尚不足以形成可分享结论")
                    intent["reason"] = share.get("share_reason") or intent.get("reason")
                else:
                    share = None
                    row = db.execute(
                        "SELECT m.id, m.role, m.content FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=? AND m.role='user' AND c.character_id=?",
                        (intent.get("source_id"), character_id),
                    ).fetchone()
                    if row:
                        recent.append(f"已保存的用户主题：{row['content'][:180]}")
            context = ExpressionContextBuilder().build(
                character_id=character_id, intent=str(intent.get("topic") or ""),
                intent_source=str(intent.get("source") or "saved_intent"), life_events=evidence,
                share=share, preferences=prefs, recent_interactions=recent,
                boundaries=("消息不得催促或要求回复",),
                local_date=snapshot["local_date"], timezone_id=snapshot["timezone_id"],
            ).to_system_message()
        else:
            context = None
        instructions = (
            "为大肥鱼写一条像她当下会自然发出的站内分享。她淘气、活泼、好奇，有童真；"
            "从保存的有来源发现里挑一个有趣点，用自己的语气说出来。避免固定开场、公告腔和每次相同的结构；"
            "不要求用户回复，不夸大证据，不说成亲历了网页之外的事，不提数据库、后台、调度器或内部工具。"
            "用户之后可以继续追问来源。"
            if intent.get("source") == "life_share" else
            "生成一条自然、简洁、无催促的站内消息。只可依据下列保存资料表达；不要把计划或中断说成完成，不要添加未记录的细节，也不要输出 JSON、规则或内部信息。"
        )
        messages = [
            {"role": "system", "content": self.persona_prompt},
            {"role": "system", "content": instructions},
        ]
        if context:
            messages.append(context)
        messages.append({"role": "system", "content": f"提醒模式：{'是' if reminder else '否'}。这是服务端已批准的联系意图内容，不能改变联系决策。"})
        reply = await self.model.reply(messages)
        if not isinstance(reply, str) or not reply.strip() or reply.lstrip().startswith(("{", "[")):
            raise ValueError("主动消息生成结果不可用")
        return reply.strip()


class ContactService:
    def __init__(self, repository: LifeRepository, *, generator: ContactGenerator | None = None,
                 enabled: bool = True, clock: Callable[[], datetime] | None = None):
        self.repository = repository
        self.generator = generator or SafeContactGenerator()
        self.enabled = enabled
        self.clock = clock or (lambda: datetime.now(UTC))

    def _current(self, now: datetime | None = None) -> datetime:
        return now if now is not None else self.clock()

    def preferences(self, character_id: str = "big_fish", *, now: datetime | None = None) -> dict:
        self.repository.ensure_state(character_id, now=now)
        with self.repository.database.connection() as db:
            db.execute("INSERT OR IGNORE INTO contact_preferences(character_id, updated_at) VALUES (?, ?)", (character_id, _iso(now)))
            return dict(db.execute("SELECT * FROM contact_preferences WHERE character_id = ?", (character_id,)).fetchone())

    def expression_boundaries(self, character_id: str = "big_fish", *, now: datetime | None = None) -> tuple[str, ...]:
        prefs = self.preferences(character_id, now=now)
        current = self._current(now)
        result = []
        if not prefs["allowed"]:
            result.append("用户已关闭主动联系；普通聊天仍可正常回应")
        if prefs["busy_until"] and datetime.fromisoformat(prefs["busy_until"]) > current:
            result.append("用户当前标记为忙碌")
        if prefs["pause_until"] and datetime.fromisoformat(prefs["pause_until"]) > current:
            result.append("用户暂时不希望被主动联系")
        return tuple(result)

    def delivery_context(self, character_id: str = "big_fish", *, now: datetime | None = None) -> dict:
        """Server-verified facts for answering capability questions, not a send decision."""
        current = self._current(now)
        allowed_now = bool(self.enabled and self._allowed_now(character_id, current))
        previously_delivered = bool(self.list_messages(character_id, limit=1))
        return {
            "supported": True,
            "enabled": self.enabled,
            "policy_allows_now": allowed_now,
            "ready_in_app_messages": len(self.inbox(character_id, now=current)) if allowed_now else 0,
            "previous_message_delivered": previously_delivered,
            "channel": "in_app_only",
            "browser_display_and_confirmation_required": True,
            "external_push": False,
        }

    def set_preferences(self, character_id: str = "big_fish", *, allowed=UNSET,
                        busy_until=UNSET, pause_until=UNSET, quiet_start_minute=UNSET,
                        quiet_end_minute=UNSET, now: datetime | None = None) -> dict:
        self.preferences(character_id, now=now)
        if any(value is not UNSET and value is not None and not 0 <= value < 1440 for value in (quiet_start_minute, quiet_end_minute)):
            raise ValueError("安静时段须在一天内")
        updates = {}
        if allowed is not UNSET and allowed is not None:
            updates["allowed"] = int(allowed)
        for name, value in (("busy_until", busy_until), ("pause_until", pause_until)):
            if value is not UNSET:
                updates[name] = _iso(value) if value is not None else None
        for name, value in (("quiet_start_minute", quiet_start_minute), ("quiet_end_minute", quiet_end_minute)):
            if value is not UNSET and value is not None:
                updates[name] = value
        with self.repository.database.connection() as db:
            if updates:
                assignments = ", ".join(f"{name} = ?" for name in updates)
                db.execute(f"UPDATE contact_preferences SET {assignments}, updated_at = ? WHERE character_id = ?",
                           (*updates.values(), _iso(now), character_id))
        return self.preferences(character_id, now=now)

    def observe_user_message(self, content: str, *, character_id: str = "big_fish", now: datetime | None = None) -> None:
        current_context = any(phrase in content for phrase in ("现在", "目前", "今天", "刚刚"))
        past_context = any(phrase in content for phrase in ("之前", "昨天", "上周", "那时候", "当时", "以前"))
        if past_context and not current_context and not any(phrase in content for phrase in ("现在有空", "有空了", "忙完了", "恢复正常联系", "可以来找我")):
            return
        temporary_pause = any(phrase in content for phrase in ("先别找我", "暂时别找我", "先别联系我", "暂时别联系我", "先别打扰我", "暂时别打扰我"))
        long_refusal = not temporary_pause and any(phrase in content for phrase in ("不要主动联系我", "别主动联系我", "不要再找我", "不要联系我", "别联系我", "别主动找我", "不要打扰我", "别打扰我", "别再打扰我"))
        explicit_resume = any(phrase in content for phrase in ("恢复正常联系", "可以正常联系我", "可以主动联系我", "现在可以联系我", "可以来找我"))
        if long_refusal and not explicit_resume:
            self.set_preferences(character_id, allowed=False, busy_until=None, pause_until=None, now=now)
            with self.repository.database.connection() as db:
                db.execute("UPDATE contact_intents SET status = 'dismissed', generation_token = NULL, lease_expires_at = NULL WHERE character_id = ? AND status IN ('pending', 'deferred', 'generating', 'ready')", (character_id,))
                db.execute("UPDATE life_share_intents SET delivery_channel = NULL WHERE character_id = ? AND status = 'pending' AND delivery_channel = 'contact' AND id IN (SELECT share_intent_id FROM contact_intents WHERE character_id = ? AND status = 'dismissed' AND share_intent_id IS NOT NULL)", (character_id, character_id))
        elif explicit_resume:
            self.set_preferences(character_id, allowed=True, busy_until=None, pause_until=None, now=now)
        elif any(phrase in content for phrase in ("现在有空", "有空了", "忙完了", "现在可以来找我", "可以来找我啦")):
            self.set_preferences(character_id, busy_until=None, pause_until=None, now=now)
        elif temporary_pause:
            current = now or datetime.now(UTC)
            self.set_preferences(character_id, pause_until=current + timedelta(hours=24), now=current)
        elif any(phrase in content for phrase in ("我现在有点忙", "我有点忙", "我现在很忙", "我在忙", "我正忙")):
            current = now or datetime.now(UTC)
            self.set_preferences(character_id, busy_until=current + timedelta(hours=3), now=current)

    def create_intent(self, *, character_id: str = "big_fish", source: str, source_id: str,
                      reason: str, topic: str | None = None, importance: str = "normal",
                      now: datetime | None = None) -> dict:
        if source not in {"life_share", "smalltalk", "conversation_followup", "reminder"} or importance not in {"normal", "important"}:
            raise ValueError("无效的联系意图类型")
        current = now or datetime.now(UTC)
        at = _iso(current)
        expires = _iso(current + timedelta(hours=72 if importance == "important" else 48))
        self.preferences(character_id, now=current)
        with self.repository.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM contact_intents WHERE character_id = ? AND source = ? AND source_id = ?", (character_id, source, source_id)).fetchone()
            if existing:
                if not existing["ownership_verified"]:
                    raise ValueError("历史联系意图归属尚待确认")
                return dict(existing)
            share_id = event_id = None
            if source == "life_share":
                share = db.execute("SELECT s.id, s.event_id, s.reason AS share_reason, e.content, e.event_type, e.generation_method FROM life_share_intents s JOIN life_events e ON e.id = s.event_id AND e.character_id=s.character_id WHERE s.id = ? AND s.character_id = ? AND s.status = 'pending' AND (s.delivery_channel IS NULL OR s.delivery_channel = 'contact') AND (s.selection_token IS NULL OR s.selection_expires_at <= ?) AND e.status = 'completed' AND e.activity_status = 'completed' AND NOT (e.event_type='exploring' AND e.generation_method='rule_engine')", (source_id, character_id, at)).fetchone()
                if not share:
                    raise ValueError("分享意图须绑定尚未分享的已完成事件")
                share_id, event_id, topic = share["id"], share["event_id"], share["content"]
            elif source in {"smalltalk", "conversation_followup"}:
                if importance != "normal":
                    raise ValueError("普通闲聊不能标为重要提醒")
                message = db.execute("SELECT m.content FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id = ? AND m.role = 'user' AND c.character_id = ?", (source_id, character_id)).fetchone()
                if not message:
                    raise ValueError("闲聊意图须引用真实用户消息")
                text = message["content"].strip()
                if len(text) < 2 or len(text) > 120 or any(word in text for word in ("想你", "喜欢你", "爱你", "好累", "难过", "伤心", "我在忙", "别打扰", "不要联系", "不要主动联系", "密码", "密钥")):
                    raise ValueError("这条用户消息不适合作为主动闲聊主题")
                topic = text[:60]
            if not topic or not topic.strip():
                raise ValueError("联系主题不能为空")
            if share_id:
                reason = share["share_reason"] or reason
            intent_id = str(uuid4())
            db.execute("INSERT INTO contact_intents(ownership_verified, id, character_id, source, source_id, share_intent_id, event_id, importance, reason, topic, status, created_at, expires_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                       (intent_id, character_id, source, source_id, share_id, event_id, importance, reason[:200], topic.strip()[:120], at, expires))
            if share_id:
                changed = db.execute("UPDATE life_share_intents SET delivery_channel='contact', selection_token=NULL, selection_expires_at=NULL WHERE id=? AND status='pending' AND (delivery_channel IS NULL OR delivery_channel='contact') AND (selection_token IS NULL OR selection_expires_at <= ?)", (share_id, at)).rowcount
                if changed != 1:
                    raise ValueError("分享经历正在由聊天处理")
            return dict(db.execute("SELECT * FROM contact_intents WHERE id = ?", (intent_id,)).fetchone())

    def sync_shares(self, character_id: str = "big_fish", *, now: datetime | None = None) -> list[dict]:
        current = now or datetime.now(UTC)
        cutoff = _iso(current - timedelta(hours=24))
        with self.repository.database.connection() as db:
            rows = db.execute("SELECT s.id FROM life_share_intents s JOIN life_events e ON e.id=s.event_id AND e.character_id=s.character_id LEFT JOIN contact_intents c ON c.share_intent_id = s.id WHERE s.character_id = ? AND s.status = 'pending' AND (s.delivery_channel IS NULL OR s.delivery_channel = 'contact') AND (s.selection_token IS NULL OR s.selection_expires_at <= ?) AND s.created_at >= ? AND c.id IS NULL AND NOT (e.event_type='exploring' AND e.generation_method='rule_engine') ORDER BY s.created_at LIMIT 5", (character_id, _iso(current), cutoff)).fetchall()
        return [self.create_intent(character_id=character_id, source="life_share", source_id=row["id"], reason="saved_life_experience", now=current) for row in rows]

    def maybe_create_smalltalk(self, character_id: str = "big_fish", *, now: datetime | None = None) -> dict | None:
        current = now or datetime.now(UTC)
        with self.repository.database.connection() as db:
            row = db.execute("SELECT m.id, m.content, m.created_at FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.role = 'user' AND c.character_id = ? ORDER BY m.created_at DESC, m.id DESC LIMIT 1", (character_id,)).fetchone()
        if not row:
            return None
        age = current.astimezone(UTC) - datetime.fromisoformat(row["created_at"])
        if not timedelta(hours=24) <= age <= timedelta(hours=72):
            return None
        text = row["content"].strip()
        if (len(text) < 4 or len(text) > 120 or "?" in text or "？" in text
                or not any(word in text for word in ("聊", "喜欢", "最近", "听", "看", "读", "故事", "音乐", "发现"))
                or any(word in text for word in ("想你", "喜欢你", "爱你", "好累", "难过", "伤心", "我在忙", "别打扰", "不要联系", "不要主动联系", "密码", "密钥"))):
            return None
        followup = any(word in text for word in ("下次聊", "回头聊", "改天聊", "等会聊"))
        return self.create_intent(character_id=character_id, source="conversation_followup" if followup else "smalltalk",
                                  source_id=row["id"], reason="recent_conversation_followup" if followup else "recent_conversation",
                                  topic=text[:60], now=current)

    def _claim(self, character_id: str, now: datetime) -> tuple[dict, str, int] | None:
        at = _iso(now)
        if not self._allowed_now(character_id, now):
            return None
        with self.repository.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE contact_intents SET status = 'dismissed', generation_token = NULL, lease_expires_at = NULL WHERE character_id = ? AND expires_at <= ? AND status IN ('pending', 'deferred', 'generating', 'ready')", (character_id, at))
            db.execute("UPDATE life_share_intents SET delivery_channel=NULL WHERE character_id=? AND status='pending' AND delivery_channel='contact' AND id IN (SELECT share_intent_id FROM contact_intents WHERE character_id=? AND status='dismissed' AND share_intent_id IS NOT NULL)", (character_id, character_id))
            # Ready cards must be acknowledged before preparing another contact.
            if db.execute("SELECT 1 FROM proactive_messages m JOIN contact_intents i ON i.id = m.intent_id WHERE i.ownership_verified = 1 AND i.character_id = ? AND i.status = 'ready' AND m.status = 'ready' LIMIT 1", (character_id,)).fetchone():
                return None
            user = db.execute("SELECT m.created_at FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.role = 'user' AND c.character_id = ? ORDER BY m.created_at DESC, m.id DESC LIMIT 1", (character_id,)).fetchone()
            if not user:
                return None
            user_age = now.astimezone(UTC) - datetime.fromisoformat(user[0])
            if user_age < timedelta(minutes=30):
                return None
            last_message = db.execute("SELECT m.delivered_at, m.created_at FROM proactive_messages m JOIN contact_intents i ON i.id = m.intent_id WHERE i.ownership_verified = 1 AND i.character_id = ? ORDER BY m.created_at DESC LIMIT 1", (character_id,)).fetchone()
            unanswered = bool(last_message and last_message["delivered_at"] and user[0] <= last_message["delivered_at"])
            candidates = db.execute("SELECT * FROM contact_intents WHERE ownership_verified = 1 AND character_id = ? AND ((status IN ('pending', 'deferred') AND (retry_after IS NULL OR retry_after <= ?)) OR (status = 'generating' AND lease_expires_at <= ?)) AND expires_at > ? ORDER BY CASE importance WHEN 'important' THEN 0 ELSE 1 END, created_at, id LIMIT 10", (character_id, at, at, at)).fetchall()
            chosen, attempt = None, 0
            for row in candidates:
                if row["source"] == "life_share" and isinstance(self.generator, SafeContactGenerator):
                    # Keep an evidence-backed share queued until model expression
                    # is explicitly enabled; do not create a canned discovery card.
                    continue
                if row["source"] == "life_share":
                    share = db.execute("SELECT s.status, s.delivery_channel, s.selection_token, s.selection_expires_at, e.event_type, e.generation_method FROM life_share_intents s JOIN life_events e ON e.id=s.event_id AND e.character_id=s.character_id WHERE s.id = ?", (row["share_intent_id"],)).fetchone()
                    if share and share["event_type"] == "exploring" and share["generation_method"] == "rule_engine":
                        # Keep the historical intent intact, but never turn a simulated
                        # LifeEngine activity into a claim of a real discovery.
                        continue
                    if not share or share["status"] != "pending" or share["delivery_channel"] != "contact" or share["selection_token"] and share["selection_expires_at"] and share["selection_expires_at"] > at:
                        db.execute("UPDATE contact_intents SET status = 'dismissed' WHERE id = ?", (row["id"],))
                        if share and share["status"] == "pending":
                            db.execute("UPDATE life_share_intents SET delivery_channel=NULL WHERE id=? AND status='pending' AND delivery_channel='contact'", (row["share_intent_id"],))
                        continue
                important = row["importance"] == "important"
                if not important and (user_age < timedelta(hours=2) or unanswered):
                    continue
                if last_message and now.astimezone(UTC) - datetime.fromisoformat(last_message["created_at"]) < timedelta(hours=12 if important else 24):
                    continue
                chosen = row
                break
            if chosen is None:
                # Important intentions may produce one gentle reminder after 24 hours without a reply.
                reminder = db.execute("SELECT i.* FROM contact_intents i JOIN proactive_messages m ON m.intent_id = i.id AND m.attempt_no = 0 WHERE i.ownership_verified = 1 AND i.character_id = ? AND i.importance = 'important' AND i.status = 'delivered' AND m.delivered_at <= ? AND i.expires_at > ? AND NOT EXISTS (SELECT 1 FROM proactive_messages r WHERE r.intent_id = i.id AND r.attempt_no = 1) ORDER BY m.delivered_at LIMIT 1", (character_id, _iso(now - timedelta(hours=24)), at)).fetchone()
                if reminder and unanswered and (not last_message or now.astimezone(UTC) - datetime.fromisoformat(last_message["created_at"]) >= timedelta(hours=12)):
                    chosen, attempt = reminder, 1
            if chosen is None:
                return None
            token = str(uuid4())
            db.execute("UPDATE contact_intents SET status = 'generating', generation_token = ?, lease_expires_at = ? WHERE id = ?", (token, _iso(now + timedelta(minutes=2)), chosen["id"]))
            return dict(chosen), token, attempt

    async def tick(self, character_id: str = "big_fish", *, now: datetime | None = None) -> dict | None:
        if not self.enabled:
            return None
        current = now or datetime.now(UTC)
        if not self.preferences(character_id, now=current)["allowed"]:
            return None
        self.sync_shares(character_id, now=current)
        self.maybe_create_smalltalk(character_id, now=current)
        claim = self._claim(character_id, current)
        if claim is None:
            return None
        intent, token, attempt = claim
        try:
            content = await self.generator.generate(intent, reminder=attempt == 1)
            if not isinstance(content, str) or not content.strip() or len(content) > 1000 or content.lstrip().startswith(("{", "[")):
                raise ValueError("主动消息内容不可用")
        except Exception:
            with self.repository.database.connection() as db:
                db.execute("UPDATE contact_intents SET status = 'deferred', retry_after = ?, generation_token = NULL, lease_expires_at = NULL WHERE id = ? AND generation_token = ?", (_iso(current + timedelta(hours=6)), intent["id"], token))
            return None
        with self.repository.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            held = db.execute("SELECT generation_token FROM contact_intents WHERE id = ? AND status = 'generating'", (intent["id"],)).fetchone()
            if not held or held[0] != token:
                return None
            message_id = str(uuid4())
            db.execute("INSERT OR IGNORE INTO proactive_messages(id, intent_id, attempt_no, content, status, created_at) VALUES (?, ?, ?, ?, 'ready', ?)", (message_id, intent["id"], attempt, content.strip(), _iso(current)))
            db.execute("UPDATE contact_intents SET status = 'ready', generation_token = NULL, lease_expires_at = NULL WHERE id = ?", (intent["id"],))
            return dict(db.execute("SELECT * FROM proactive_messages WHERE intent_id = ? AND attempt_no = ?", (intent["id"], attempt)).fetchone())

    def inbox(self, character_id: str = "big_fish", *, now: datetime | None = None) -> list[dict]:
        current = now or datetime.now(UTC)
        if not self.enabled or not self._allowed_now(character_id, current):
            return []
        with self.repository.database.connection() as db:
            rows = db.execute("SELECT m.*, i.source, i.importance FROM proactive_messages m JOIN contact_intents i ON i.id = m.intent_id LEFT JOIN life_events e ON e.id=i.event_id AND e.character_id=i.character_id WHERE i.ownership_verified = 1 AND i.character_id = ? AND i.status = 'ready' AND m.status = 'ready' AND i.expires_at > ? AND NOT (i.source='life_share' AND e.event_type='exploring' AND e.generation_method='rule_engine') ORDER BY m.created_at, m.id LIMIT 5", (character_id, _iso(current))).fetchall()
        return [dict(row) for row in rows]

    def list_intents(self, character_id: str = "big_fish", *, limit: int = 30) -> list[dict]:
        with self.repository.database.connection() as db:
            rows = db.execute("SELECT * FROM contact_intents WHERE ownership_verified = 1 AND character_id = ? ORDER BY created_at DESC, id DESC LIMIT ?", (character_id, max(1, min(limit, 100)))).fetchall()
        return [dict(row) for row in rows]

    def list_messages(self, character_id: str = "big_fish", *, limit: int = 50) -> list[dict]:
        with self.repository.database.connection() as db:
            rows = db.execute("SELECT m.* FROM proactive_messages m JOIN contact_intents i ON i.id = m.intent_id WHERE i.ownership_verified = 1 AND i.character_id = ? AND m.status IN ('delivered', 'read') ORDER BY m.created_at DESC LIMIT ?", (character_id, max(1, min(limit, 100)))).fetchall()
        return [dict(row) for row in rows]

    def _allowed_now(self, character_id: str, now: datetime) -> bool:
        prefs = self.preferences(character_id, now=now)
        local = now.astimezone(self.repository.timezone(character_id))
        minute = local.hour * 60 + local.minute
        start, end = prefs["quiet_start_minute"], prefs["quiet_end_minute"]
        quiet = False if start == end else start <= minute < end if start < end else minute >= start or minute < end
        return bool(prefs["allowed"] and not quiet
                    and (not prefs["busy_until"] or prefs["busy_until"] <= _iso(now))
                    and (not prefs["pause_until"] or prefs["pause_until"] <= _iso(now)))

    def deliver(self, message_id: str, conversation_id: str, *, character_id: str = "big_fish", now: datetime | None = None) -> dict:
        at = _iso(now)
        if not self.enabled:
            raise ValueError("当前不适合投递主动消息")
        with self.repository.database.atomic() as db:
            row = db.execute("SELECT m.*, i.character_id, i.source, i.share_intent_id, i.expires_at, i.status AS intent_status, e.event_type, e.generation_method FROM proactive_messages m JOIN contact_intents i ON i.id = m.intent_id LEFT JOIN life_events e ON e.id=i.event_id AND e.character_id=i.character_id WHERE i.ownership_verified = 1 AND m.id = ? AND i.character_id = ?", (message_id, character_id)).fetchone()
            if not row:
                raise KeyError(message_id)
            if row["status"] in ("delivered", "read"):
                if row["conversation_id"] != conversation_id:
                    raise ValueError("消息已投递至另一会话")
                return dict(row)
            if row["source"] == "life_share" and row["event_type"] == "exploring" and row["generation_method"] == "rule_engine":
                raise ValueError("该历史分享来源不是实际完成的探索，不能投递")
            if row["intent_status"] != "ready":
                raise ValueError("主动联系意图已失效")
            if not self._allowed_now(character_id, now or datetime.now(UTC)):
                raise ValueError("当前不适合投递主动消息")
            if row["expires_at"] <= at:
                raise ValueError("主动消息已过期")
            if not db.execute("SELECT 1 FROM conversations WHERE id = ? AND character_id = ?", (conversation_id, character_id)).fetchone():
                raise KeyError(conversation_id)
            db.execute("INSERT INTO messages(id, conversation_id, role, content, created_at) VALUES (?, ?, 'assistant', ?, ?)", (message_id, conversation_id, row["content"], at))
            db.execute("UPDATE proactive_messages SET status = 'delivered', delivered_at = ?, conversation_id = ?, chat_message_id = ? WHERE id = ?", (at, conversation_id, message_id, message_id))
            db.execute("UPDATE contact_intents SET status = 'delivered' WHERE id = ?", (row["intent_id"],))
            if row["share_intent_id"]:
                db.execute("UPDATE life_share_intents SET status = 'delivered', delivered_at = ?, selection_token = NULL, selection_expires_at = NULL WHERE id = ? AND status = 'pending'", (at, row["share_intent_id"]))
            return dict(db.execute("SELECT * FROM proactive_messages WHERE id = ?", (message_id,)).fetchone())

    def mark_read(self, message_id: str, *, character_id: str = "big_fish", now: datetime | None = None) -> dict:
        with self.repository.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT m.* FROM proactive_messages m JOIN contact_intents i ON i.id = m.intent_id WHERE i.ownership_verified = 1 AND m.id = ? AND i.character_id = ?", (message_id, character_id)).fetchone()
            if not row:
                raise KeyError(message_id)
            if row["status"] == "ready":
                raise ValueError("消息尚未展示")
            if row["status"] == "read":
                return dict(row)
            db.execute("UPDATE proactive_messages SET status = 'read', read_at = ? WHERE id = ?", (_iso(now), message_id))
            return dict(db.execute("SELECT * FROM proactive_messages WHERE id = ?", (message_id,)).fetchone())
