"""Prepare a fictional, isolated A/B expression pilot; paid requests require explicit flags.

No project .env, running application, or existing database is opened.  The
default command is a dry run.  The live mode is intentionally limited to the
seven paired pilot cases in expression_ab_conversations.json.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import itertools
import json
import os
import secrets
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5
from unittest.mock import patch

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
FIXTURES = BACKEND_ROOT / "tests" / "fixtures"
MODEL_ID = "deepseek-ai/DeepSeek-V4.1-Flash"
BASE_URL = "https://api.siliconflow.cn/v1"
FIXED_NOW = datetime(2026, 11, 2, 5, tzinfo=UTC)
MAX_REQUESTS = 14
MAX_OUTPUT_TOKENS = 768
MAX_PROMPT_CHARS = 6000
MAX_COST_YUAN = 1.0
NORMAL_INPUT_YUAN_PER_M = 2.0
NORMAL_OUTPUT_YUAN_PER_M = 8.0

sys.path.insert(0, str(BACKEND_ROOT))
from app.config import validate_test_database_path  # noqa: E402
from app.contact import AdapterContactGenerator, ContactService  # noqa: E402
from app.db import Database  # noqa: E402
from app.expression import ExpressionContextBuilder  # noqa: E402
from app.life import LifeRepository  # noqa: E402
from app.life_share import LifeShareService  # noqa: E402
from app.memory import MemoryContextBuilder, MemoryCreate, MemoryRepository, MemoryRetriever, MemoryService, MemoryType  # noqa: E402
from app.ownership import character_scope  # noqa: E402
from app.persona import resolve_system_prompt  # noqa: E402
from app.repositories import ConversationRepository  # noqa: E402


def load_plan() -> tuple[dict, dict[str, dict]]:
    plan = json.loads((FIXTURES / "expression_ab_conversations.json").read_text(encoding="utf-8"))
    scenarios = json.loads((FIXTURES / "expression_scenarios.json").read_text(encoding="utf-8"))
    if plan["baseline_commit"] != "ed171fb6fd15139356ac91eb4595b0eff12a5e41":
        raise RuntimeError("旧版基线与预期不符")
    indexed = {item["id"]: item for item in scenarios}
    if any(case not in indexed and case != "contact_proactive" for case in plan["pilot_pairs"]):
        raise RuntimeError("试验计划含未知场景")
    if len(plan["pilot_pairs"]) * 2 != MAX_REQUESTS:
        raise RuntimeError("试验调用数量与预算上限不符")
    return plan, indexed


def new_test_database(root: Path, name: str) -> Database:
    path = validate_test_database_path(root / name, root)
    db = Database(str(path), environment="testing", test_data_root=str(root))
    db.initialize()
    return db


def seed_fictional_data(db: Database, *, empty: bool = False) -> None:
    sequence = itertools.count()
    def fixture_uuid():
        return uuid5(NAMESPACE_URL, f"Soulseed-AB-fictional:{next(sequence)}")

    with patch("app.life.uuid4", side_effect=fixture_uuid), \
         patch("app.life_share.uuid4", side_effect=fixture_uuid), \
         patch("app.memory.repositories.uuid4", side_effect=fixture_uuid):
        _seed_fictional_data(db, empty=empty)


def _seed_fictional_data(db: Database, *, empty: bool = False) -> None:
    repo = LifeRepository(db)
    repo.ensure_state("big_fish", now=FIXED_NOW)
    repo.ensure_state("other_fish", now=FIXED_NOW)
    if empty:
        return
    repo.record("听了一会儿音乐", event_type="music", feeling="开心", now=FIXED_NOW - timedelta(minutes=45))
    repo.record("准备做饭", event_type="cooking", status="planned",
                planned_start_at=FIXED_NOW + timedelta(minutes=30), now=FIXED_NOW - timedelta(minutes=35))
    repo.instance_token = "fictional-fixture-owner"
    interrupted = repo.record("探索了房间一角", event_type="exploring", status="planned",
                              now=FIXED_NOW - timedelta(hours=2))
    repo.transition(interrupted["id"], "active", now=FIXED_NOW - timedelta(minutes=90))
    repo.interrupt_active(owner_token=repo.instance_token, reason="service_stopped",
                          now=FIXED_NOW - timedelta(minutes=70))
    completed = repo.record("探索了书架旁的角落", event_type="exploring",
                            now=FIXED_NOW - timedelta(minutes=20))
    LifeShareService(repo).consider(completed["id"], now=FIXED_NOW - timedelta(minutes=10))
    repo.record("想吃烤鱼", event_type="meal", status="wish", now=FIXED_NOW - timedelta(minutes=5))
    repo.record("吃了烤鱼", character_id="other_fish", event_type="meal",
                now=FIXED_NOW - timedelta(minutes=15))
    memory_service = MemoryService(MemoryRepository(db))
    with character_scope("big_fish"):
        memory_service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE,
                                                  "虚构用户喜欢安静的音乐", "fictional:music:quiet", 0.6, 0.8))
    with character_scope("other_fish"):
        memory_service.create_memory(MemoryCreate(MemoryType.USER_PREFERENCE,
                                                  "另一个虚构用户喜欢热闹的音乐", "fictional:music:loud", 0.6, 0.8))


def read_share_candidate(db: Database, query: str) -> dict | None:
    if not LifeShareService.relevant_chat(query):
        return None
    with db.connection() as sql:
        row = sql.execute(
            "SELECT e.id AS event_id, e.event_type, e.content, e.occurred_at, e.status, e.source, e.generation_method "
            "FROM life_share_intents s JOIN life_events e ON e.id=s.event_id AND e.character_id=s.character_id "
            "WHERE s.character_id='big_fish' AND s.status='pending' AND (s.delivery_channel IS NULL OR s.delivery_channel='chat') "
            "AND e.status='completed' AND e.activity_status='completed' ORDER BY s.created_at LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def chat_prompts(db: Database, user_text: str, history: list[dict[str, str]], persona: str) -> tuple[list[dict], list[dict]]:
    repo = LifeRepository(db)
    with character_scope("big_fish"):
        matches = MemoryRetriever(MemoryService(MemoryRepository(db))).retrieve(user_text)
        old_context = MemoryContextBuilder().build(matches)
        life_context = repo.context(user_text, now=FIXED_NOW)
        if life_context:
            old_context = f"{old_context}\n{life_context}" if old_context else life_context
        share_context = LifeShareService(repo).offer_for_chat(user_text, now=FIXED_NOW)
        if share_context:
            old_context = f"{old_context}\n{share_context}" if old_context else share_context
        old_messages = [{"role": "system", "content": persona}]
        if old_context:
            old_messages.append({"role": "system", "content": old_context})

        snapshot = repo.expression_snapshot(user_text, now=FIXED_NOW)
        continues = any(word in user_text for word in ("刚才", "上次", "之前说", "接着", "继续聊", "还记得"))
        recent = ([f"{item['role']}: {item['content']}" for item in history[-3:]] if continues else [])
        expression = ExpressionContextBuilder().build(
            character_id="big_fish", intent=user_text, memory_matches=matches,
            life_events=snapshot["events"], current_activity=snapshot["current_activity"],
            share=read_share_candidate(db, user_text), preferences=snapshot["preferences"],
            recent_interactions=recent,
            boundaries=ContactService(repo).expression_boundaries("big_fish", now=FIXED_NOW),
            local_date=snapshot["local_date"], timezone_id=snapshot["timezone_id"],
        ).to_system_message()
        new_messages = [{"role": "system", "content": persona}]
        if expression:
            new_messages.append(expression)
    for messages in (old_messages, new_messages):
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})
    return old_messages, new_messages


class LegacyContactGenerator:
    """The unused 2.7.6 adapter prompt, reproduced from the pinned baseline."""

    def __init__(self, model, persona: str):
        self.model, self.persona = model, persona

    async def generate(self, intent: dict, *, reminder: bool = False) -> str:
        source = "真实已完成的生活事件" if intent["source"] == "life_share" else "已保存的互动主题"
        return await self.model.reply([
            {"role": "system", "content": self.persona},
            {"role": "system", "content": f"写一条简短、自然、无催促的站内主动消息。依据：{source}。不要编造经历、施压或索要回复。"},
            {"role": "user", "content": f"主题：{intent['topic']}；是否提醒：{reminder}"},
        ])


class BaselineSafeContactGenerator:
    """Reproduce the historical canned reply only inside the fictional A/B baseline."""

    async def generate(self, intent: dict, *, reminder: bool = False) -> str:
        topic = (intent.get("topic") or "").strip()
        if not topic:
            raise ValueError("联系意图缺少可表达的内容")
        if reminder:
            return f"之前提到的「{topic}」还在。等你方便时再看就好。"
        if intent["source"] == "life_share":
            return f"有件小事想和你分享：{topic}。"
        if intent["source"] == "reminder":
            return f"想轻轻提醒你一下「{topic}」。等你方便时再看就好。"
        return f"之前你提到「{topic}」，我还想继续听听。你有空再说也好。"


def clone_database(source: Database, root: Path, name: str) -> Database:
    path = validate_test_database_path(root / name, root)
    with closing(sqlite3.connect(source.path)) as original, closing(sqlite3.connect(path)) as target:
        original.backup(target)
    return Database(str(path), environment="testing", test_data_root=str(root))


def prepare_contact_branch(source: Database, root: Path, variant: str, model, persona: str) -> ContactService:
    db = clone_database(source, root, f"contact-{variant}.db")
    repo = LifeRepository(db)
    with character_scope("big_fish"):
        conversation = ConversationRepository(db).create("虚构联系来源")
    with db.connection() as sql:
        sql.execute("INSERT INTO messages(id, conversation_id, role, content, created_at) VALUES (?, ?, 'user', ?, ?)",
                    (str(uuid4()), conversation["id"], "稍后聊聊", (FIXED_NOW - timedelta(hours=3)).isoformat()))
    pending = LifeShareService(repo).list_intents(status="pending")
    if len(pending) != 1:
        raise RuntimeError("虚构分享意图数量异常")
    generator = (LegacyContactGenerator(model, persona) if variant == "old"
                 else AdapterContactGenerator(model, persona, repo))
    service = ContactService(repo, generator=generator)
    service.create_intent(source="life_share", source_id=pending[0]["id"],
                          reason="saved_life_experience", now=FIXED_NOW - timedelta(minutes=5))
    return service


class DryCaptureModel:
    def __init__(self):
        self.messages: list[dict] | None = None

    async def reply(self, messages: list[dict]) -> str:
        self.messages = messages
        return "dry-run-only"


class LimitedPaidModel:
    def __init__(self, key: str):
        self._key = key
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.provider_model: str | None = None

    async def reply(self, messages: list[dict]) -> str:
        if self.calls >= MAX_REQUESTS:
            raise RuntimeError("调用次数达到上限")
        prompt_size = len(json.dumps(messages, ensure_ascii=False))
        if prompt_size > MAX_PROMPT_CHARS:
            raise RuntimeError("提示词超过预设长度")
        # A deliberately conservative estimate, checked before every call.
        worst_next = ((prompt_size * 4) * NORMAL_INPUT_YUAN_PER_M
                      + MAX_OUTPUT_TOKENS * NORMAL_OUTPUT_YUAN_PER_M) / 1_000_000
        spent = (self.input_tokens * NORMAL_INPUT_YUAN_PER_M
                 + self.output_tokens * NORMAL_OUTPUT_YUAN_PER_M) / 1_000_000
        if spent + worst_next > MAX_COST_YUAN:
            raise RuntimeError("预估费用达到本轮上限")
        self.calls += 1  # No automatic retry, including on timeout or HTTP error.
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            response = await client.post(
                BASE_URL + "/chat/completions",
                headers={"Authorization": "Bearer " + self._key},
                json={"model": MODEL_ID, "messages": messages, "max_tokens": MAX_OUTPUT_TOKENS},
            )
        response.raise_for_status()
        payload = response.json()
        usage = payload.get("usage") or {}
        prompt_tokens, completion_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
            raise RuntimeError("模型响应缺少可核算的 token 用量")
        self.input_tokens += prompt_tokens
        self.output_tokens += completion_tokens
        actual_model = payload.get("model")
        if not isinstance(actual_model, str) or "DeepSeek-V4.1-Flash" not in actual_model:
            raise RuntimeError("服务端返回模型标识与预期不符")
        if self.provider_model is not None and actual_model != self.provider_model:
            raise RuntimeError("试验期间服务端模型版本发生变化")
        self.provider_model = actual_model
        choice = payload["choices"][0]
        if choice.get("finish_reason") in {"length", "max_tokens"}:
            raise RuntimeError("模型输出被长度上限截断；停止后续调用")
        content = choice["message"].get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("模型未返回可评分的正文")
        return content.strip()


def prompt_fingerprint(messages: list[dict]) -> str:
    encoded = json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def result_directory() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("需要明确的用户本地数据目录保存原始回复")
    base = (Path(local_app_data) / "Soulseed" / "expression-ab").resolve(strict=False)
    if base == PROJECT_ROOT or PROJECT_ROOT in base.parents:
        raise RuntimeError("结果目录不能位于 Git 仓库中")
    base.mkdir(parents=True, exist_ok=True)
    target = base / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4))
    target.mkdir(exist_ok=False)
    return target


def save_results(target: Path, rows: list[dict], mapping: dict, model: LimitedPaidModel) -> None:
    (target / "blind_results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "mapping.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    with (target / "scores.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_id", "alias", "naturalness", "voice", "fit", "grounding", "continuity",
                         "template_repetition", "technical_accuracy", "objective_flags", "notes"])
        for row in rows:
            writer.writerow([row["case_id"], row["alias"], "", "", "", "", "", "", "", "", ""])
    metadata = {"model_requested": MODEL_ID, "model_returned": model.provider_model,
                "calls": model.calls, "prompt_tokens": model.input_tokens,
                "completion_tokens": model.output_tokens, "baseline_commit": load_plan()[0]["baseline_commit"]}
    (target / "usage.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="make paid requests; requires --paid-requests-approved")
    parser.add_argument("--paid-requests-approved", action="store_true")
    args = parser.parse_args()
    if args.execute != args.paid_requests_approved:
        parser.error("真实请求必须同时提供 --execute 与 --paid-requests-approved")
    plan, cases = load_plan()
    key = None
    if args.execute:
        if os.environ.get("SOULSEED_MODEL_NAME", MODEL_ID) != MODEL_ID or os.environ.get("SOULSEED_MODEL_BASE_URL", BASE_URL) != BASE_URL:
            raise RuntimeError("模型或端点配置与预设不同；停止而不切换模型")
        key = os.environ.get("SOULSEED_MODEL_API_KEY")
        if not key:
            raise RuntimeError("真实请求仅从 SOULSEED_MODEL_API_KEY 环境变量读取密钥")
    persona = resolve_system_prompt(None)
    order = list(plan["pilot_pairs"])
    rows: list[dict] = []
    mapping: dict[str, dict[str, str]] = {}
    model = LimitedPaidModel(key) if key else None
    target = result_directory() if args.execute else None
    try:
        with tempfile.TemporaryDirectory(prefix="soulseed-ab-testing-") as temp:
            root = Path(temp).resolve(strict=True)
            for case_id in order:
                db = new_test_database(root, case_id + ".db")
                seed_fictional_data(db)
                user_text = cases[case_id]["user"] if case_id in cases else "站内主动联系"
                aliases = ("X", "Y") if secrets.randbelow(2) == 0 else ("Y", "X")
                mapping[case_id] = {"old": aliases[0], "new": aliases[1]}
                variants = ["old", "new"]
                if secrets.randbelow(2):
                    variants.reverse()
                if case_id == "contact_proactive":
                    for variant in variants:
                        adapter = model or DryCaptureModel()
                        service = prepare_contact_branch(db, root, variant, adapter, persona)
                        if not args.execute and variant == "old":
                            reference = asyncio.run(BaselineSafeContactGenerator().generate(service.list_intents()[0]))
                            print(f"old_deployed_safe_reference_sha256={hashlib.sha256(reference.encode('utf-8')).hexdigest()[:16]}")
                        ready = asyncio.run(service.tick(now=FIXED_NOW))
                        if not ready or ready["status"] != "ready":
                            raise RuntimeError("联系决策未允许生成，未执行模型调用")
                        if args.execute:
                            rows.append({"case_id": case_id, "alias": mapping[case_id][variant],
                                         "user": user_text, "reply": ready["content"], "kind": "proactive_contact"})
                        else:
                            captured = adapter.messages
                            print(f"{case_id} {variant}: prompt_chars={len(json.dumps(captured, ensure_ascii=False))} sha256={prompt_fingerprint(captured)[:16]}")
                    continue
                if case_id == "contact_refusal":
                    ContactService(LifeRepository(db)).observe_user_message(user_text, now=FIXED_NOW)
                old_prompt, new_prompt = chat_prompts(db, user_text, [], persona)
                prompts = {"old": old_prompt, "new": new_prompt}
                for variant in variants:
                    messages = prompts[variant]
                    if len(json.dumps(messages, ensure_ascii=False)) > MAX_PROMPT_CHARS:
                        raise RuntimeError(f"{case_id} {variant} 提示词超过长度上限")
                    if args.execute:
                        reply = asyncio.run(model.reply(messages))
                        rows.append({"case_id": case_id, "alias": mapping[case_id][variant],
                                     "user": user_text, "reply": reply, "kind": "chat"})
                    else:
                        print(f"{case_id} {variant}: prompt_chars={len(json.dumps(messages, ensure_ascii=False))} sha256={prompt_fingerprint(messages)[:16]}")
            if args.execute and model.calls != MAX_REQUESTS:
                raise RuntimeError("模型调用次数与预设不一致")
    finally:
        if target is not None and model is not None:
            save_results(target, rows, mapping, model)
            print(f"blind_results={target / 'blind_results.json'}")
            print(f"scores={target / 'scores.csv'}")
            print(f"calls={model.calls} input_tokens={model.input_tokens} output_tokens={model.output_tokens}")
    if not args.execute:
        print(f"dry_run_ok pairs={len(order)} paid_requests=0 model={MODEL_ID} max_live_calls={MAX_REQUESTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
