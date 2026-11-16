import asyncio
import json
from datetime import UTC, datetime, timedelta

from app.chat_service import ChatService
from app.db import Database
from app.expression import ExpressionContextBuilder
from app.life import LifeRepository
from app.life_engine import ACTIVITIES, LifeEngine
from app.ownership import character_scope
from app.autonomous_intent import build_snapshot, dry_run, generate_candidates
from app.proactive_expression import PersonaProfile, build_draft_context
from app.contact import ContactService
from app.repositories import ConversationRepository, MessageRepository


def _experience(repo, engine, character, activity, feeling, at):
    event = repo.record(
        f"{activity}了一会儿", character_id=character, event_type=activity,
        feeling=feeling, now=at,
    )
    engine.learn_from_completion(event, at)
    return event


def _profile(repo, character):
    return {item["activity_type"]: item for item in repo.preference_growth(character)}


def test_growth_forms_gradually_and_reversal_survives_restart(tmp_path):
    db = Database(str(tmp_path / "growth-reversal.db"))
    db.initialize()
    repo = LifeRepository(db)
    engine = LifeEngine(repo, seed=19)
    at = datetime(2026, 11, 1, 8, tzinfo=UTC)

    _experience(repo, engine, "fish", "music", "开心", at)
    assert "music" not in _profile(repo, "fish")
    _experience(repo, engine, "fish", "music", "喜欢", at + timedelta(days=1))
    assert _profile(repo, "fish")["music"]["tendency"] == "forming"
    _experience(repo, engine, "fish", "music", "满足", at + timedelta(days=2))
    assert _profile(repo, "fish")["music"]["tendency"] == "established"
    grown_weight = engine.preferences("fish")["music"]

    for index in range(3):
        _experience(repo, engine, "fish", "music", "疲惫", at + timedelta(days=3 + index))
    assert _profile(repo, "fish")["music"]["tendency"] == "waning"
    assert engine.preferences("fish")["music"] < grown_weight
    for index in range(12):
        _experience(repo, engine, "fish", "music", "疲惫", at + timedelta(days=6 + index))
    assert _profile(repo, "fish")["music"]["tendency"] == "cooling"
    for index in range(3):
        _experience(repo, engine, "fish", "music", "开心", at + timedelta(days=18 + index))
    assert _profile(repo, "fish")["music"]["tendency"] == "recovering"

    restarted = LifeRepository(Database(db.path))
    assert _profile(restarted, "fish")["music"] == _profile(repo, "fish")["music"]
    assert LifeEngine(restarted).preferences("fish")["music"] == engine.preferences("fish")["music"]


def test_music_and_reading_experience_histories_change_life_choice(tmp_path):
    db = Database(str(tmp_path / "growth-choices.db"))
    db.initialize()
    repo = LifeRepository(db)
    at = datetime(2026, 11, 2, 8, tzinfo=UTC)
    for character, activity in (("music_fish", "music"), ("reading_fish", "reading")):
        engine = LifeEngine(repo, seed=71)
        for index in range(16):
            _experience(repo, engine, character, activity, "开心", at + timedelta(minutes=index))
        for index, neutral in enumerate(("tidying", "cooking", "resting", "exploring", "daydreaming")):
            repo.record("普通日常", character_id=character, event_type=neutral,
                        feeling="平静", now=at + timedelta(days=1, minutes=index))

    music_profile, reading_profile = _profile(repo, "music_fish"), _profile(repo, "reading_fish")
    assert music_profile["music"]["tendency"] == "established"
    assert reading_profile["reading"]["tendency"] == "established"
    assert engine.preferences("music_fish")["music"] > engine.preferences("reading_fish")["music"]
    assert engine.preferences("reading_fish")["reading"] > engine.preferences("music_fish")["reading"]

    choices = {}
    for seed in range(200):
        chooser = LifeEngine(repo, seed=seed)
        choices["music_fish"] = chooser.choose("music_fish", now=at + timedelta(days=2))[0]
        choices["reading_fish"] = chooser.choose("reading_fish", now=at + timedelta(days=2))[0]
        if choices == {"music_fish": "music", "reading_fish": "reading"}:
            break
    assert choices == {"music_fish": "music", "reading_fish": "reading"}, choices


def test_chat_model_gets_own_growth_context_and_expresses_distinct_interests(tmp_path):
    db = Database(str(tmp_path / "growth-chat.db"))
    db.initialize()
    repo = LifeRepository(db)
    at = datetime.now(UTC) - timedelta(days=10)
    engine = LifeEngine(repo)
    for character, activity in (("music_fish", "music"), ("reading_fish", "reading")):
        for index in range(5):
            _experience(repo, engine, character, activity, "喜欢", at + timedelta(minutes=index))

    class LocalModel:
        def __init__(self):
            self.prompts = []
            self.outputs = []

        async def reply(self, prompt):
            self.prompts.append(prompt)
            rendered = next(item["content"] for item in prompt if item["role"] == "system" and "<expression_context" in item["content"])
            payload = json.loads(rendered.split("\n{", 1)[1].rsplit("\n</expression_context>", 1)[0].join(["{", ""]))
            tendency = payload["personal_growth"][0]["activity_type"]
            reply = "我最近老想听点音乐，节奏一响就开心。" if tendency == "music" else "我最近总想翻翻书，故事好像能装下好多小世界。"
            self.outputs.append(reply)
            return reply

    model = LocalModel()
    conversations, messages = ConversationRepository(db), MessageRepository(db)
    chat = ChatService(conversations, messages, model, "稳定的大肥鱼人格", life_repository=repo)
    replies = {}
    for character in ("music_fish", "reading_fish"):
        with character_scope(character):
            conversation = conversations.create()
            replies[character] = asyncio.run(chat.send(conversation["id"], "你最近喜欢什么呀？"))["content"]

    assert "音乐" in replies["music_fish"]
    assert "书" in replies["reading_fish"]
    for prompt, activity in zip(model.prompts, ("music", "reading")):
        rendered = next(item["content"] for item in prompt if item["role"] == "system" and "<expression_context" in item["content"])
        assert f'"activity_type":"{activity}"' in rendered


def test_expression_growth_payload_is_role_owned_and_bounded():
    growth = [{
        "activity_type": kind, "activity_name": ACTIVITIES[kind][0], "tendency": "established",
        "completed_experiences": 8, "positive_experiences": 8,
        "negative_experiences": 0, "net_change": 0.24, "recent_tendency": "strengthening",
    } for kind in ("music", "reading")]
    rendered = ExpressionContextBuilder(max_chars=1200).build(
        character_id="fish", intent="随便聊聊", personal_growth=growth,
    ).to_system_message()
    assert rendered and "角色本人" in rendered["content"]
    payload = json.loads(rendered["content"].split("\n{", 1)[1].rsplit("\n</expression_context>", 1)[0].join(["{", ""]))
    assert [item["activity_type"] for item in payload["personal_growth"]] == ["music", "reading"]
    assert all("net_change" not in item for item in payload["personal_growth"])


def test_autonomous_self_social_uses_same_personal_growth_context_without_recent_chat(tmp_path):
    db = Database(str(tmp_path / "growth-autonomous.db"))
    db.initialize()
    repo = LifeRepository(db)
    engine = LifeEngine(repo)
    at = datetime.now(UTC) - timedelta(days=2)
    contexts = {}
    for character, activity in (("music_fish", "music"), ("reading_fish", "reading")):
        repo.ensure_state(character, now=at)
        with db.connection() as sql:
            sql.execute("UPDATE characters SET owner_user_id='local',ownership_status='owned' WHERE id=?", (character,))
        for index in range(5):
            _experience(repo, engine, character, activity, "开心", at + timedelta(minutes=index))
        ContactService(repo).set_preferences(character, allowed=True, quiet_start_minute=0,
                                             quiet_end_minute=0, now=at + timedelta(days=1))
        with character_scope(character):
            snapshot = build_snapshot(db, character, now=at + timedelta(days=1, hours=1))
            decision = dry_run(snapshot, include_self_social=True)
            candidate = next(item for item in generate_candidates(snapshot, include_self_social=True)
                             if item.kind == "self_social")
            context = build_draft_context(
                db, character, candidate, decision,
                PersonaProfile(character, "淘气、活泼、好奇，童真稳定。", ("curious",)),
                now=at + timedelta(days=1, hours=1), include_self_social=True, life_repository=repo,
            )
        assert not snapshot.interactions
        assert decision.result.value == "CONTACT"
        contexts[character] = context.expression.to_system_message()["content"]

    assert '"activity_type":"music"' in contexts["music_fish"]
    assert '"activity_type":"reading"' in contexts["reading_fish"]
