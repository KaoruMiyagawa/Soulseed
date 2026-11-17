import asyncio
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.chat_service import ChatService
from app.db import Database
from app.desktop_contact import DesktopContact
from app.life import LifeRepository
from app.life_engine import LifeEngine
from app.life_scheduler import LifeScheduler
from app.repositories import ConversationRepository, MessageRepository

from .test_desktop_contact import setup_desktop


SHANGHAI = ZoneInfo("Asia/Shanghai")
BASE = datetime(2026, 11, 1, tzinfo=SHANGHAI)
MORNING = datetime(2026, 11, 16, 9, tzinfo=SHANGHAI)
EVENING = MORNING.replace(hour=20)


def record_series(life, kind, hour, *, start=BASE, days=8, character="big_fish"):
    for day in range(days):
        at = start.replace(hour=hour) + timedelta(days=day)
        life.record(kind, event_type=kind, character_id=character, now=at)


def seed_history(life, profile, *, start=BASE):
    if profile == "A":
        record_series(life, "reading", 20, start=start)
    elif profile == "B":
        record_series(life, "music", 9, start=start)
    else:
        for day, (kind, hour) in enumerate(zip(
            ("reading", "music", "exploring", "resting", "reading", "music", "exploring", "resting"),
            (20, 9, 13, 20, 9, 20, 9, 13),
        )):
            record_series(life, kind, hour, start=start + timedelta(days=day), days=1)


def test_habits_change_choices_only_at_their_local_time_and_plans_keep_priority(tmp_path):
    choices = {}
    for profile in ("A", "B", "C"):
        db = Database(str(tmp_path / f"{profile}.db"))
        db.initialize()
        life = LifeRepository(db)
        seed_history(life, profile)
        # Hold recent activity penalties and baseline weights constant.
        for day, kind in enumerate(("resting", "cooking", "tidying"), start=10):
            record_series(life, kind, 14, start=BASE + timedelta(days=day), days=1)
        engine = LifeEngine(life, seed=4)
        for kind in ("reading", "music"):
            engine.set_preference("big_fish", kind, 1.5, now=MORNING)
        assert life.preference_growth() == []
        habits = life.habits(now=MORNING)
        expected = {"A": [("reading", "晚上")], "B": [("music", "早上")], "C": []}[profile]
        assert [(h.activity_type, h.period) for h in habits if h.status == "established"] == expected
        assert all(h.status == "established" and h.matching_days == h.observed_days == 8 for h in habits)
        choices[profile] = [engine.choose("big_fish", now=time) for time in (MORNING, EVENING)]
        assert LifeRepository(Database(db.path)).habits(now=MORNING) == habits
        if profile == "A":
            life.record("刚读完", event_type="reading", now=EVENING - timedelta(minutes=30))
            assert engine.choose("big_fish", now=EVENING)[0] == "music"
            plan = life.record("先完成计划", event_type="tidying", status="planned", now=MORNING)
            [started] = LifeScheduler(life, seed=4).tick(now=EVENING)
            assert started["id"] == plan["id"]
            assert started["activity_status"] == "active"
        elif profile == "B":
            [started] = LifeScheduler(life, seed=4).tick(now=MORNING)
            assert started["event_type"] == "music"
            assert "habit_bonus=0.240" in started["selection_reason"]

    assert {profile: [kind for kind, _ in result] for profile, result in choices.items()} == {
        "A": ["reading", "reading"], "B": ["music", "music"], "C": ["reading", "music"],
    }
    assert "habit_bonus=0.240" in choices["A"][1][1]
    assert "habit_bonus=0.240" in choices["B"][0][1]
    for reason in (choices["A"][0][1], choices["B"][1][1], *(reason for _, reason in choices["C"])):
        assert "habit_bonus" not in reason


def test_habit_forms_weakens_and_moves_after_new_evidence_without_offline_decay(tmp_path):
    db = Database(str(tmp_path / "movement.db"))
    db.initialize()
    life = LifeRepository(db)
    record_series(life, "reading", 20)
    as_of = lambda days: BASE + timedelta(days=days)
    assert life.habits(now=as_of(1)) == ()
    assert life.habits(now=as_of(3))[0].status == "forming"
    established = life.habits(now=as_of(8))
    assert [(h.period, h.status) for h in established] == [("晚上", "established")]
    assert life.habits(now=as_of(108)) == established
    assert len(life.list_events()) == 8

    record_series(life, "reading", 9, start=as_of(108), days=17)
    shifted = life.habits(now=as_of(113))
    assert [(h.period, h.status, h.matching_days) for h in shifted] == [
        ("早上", "established", 5), ("晚上", "weakening", 3),
    ]
    assert 0 < shifted[1].choice_bonus < shifted[0].choice_bonus
    assert LifeRepository(Database(db.path)).habits(now=as_of(113)) == shifted
    assert [(h.period, h.status) for h in life.habits(now=as_of(125))] == [("早上", "established")]


def test_only_completed_occurrences_count_once_per_local_date(tmp_path):
    db = Database(str(tmp_path / "occurrences.db"))
    db.initialize()
    life = LifeRepository(db)
    life.set_timezone("fish", "America/New_York")
    ny = ZoneInfo("America/New_York")
    evening = datetime(2026, 10, 1, 20, tzinfo=ny)
    save_at = evening + timedelta(days=12)
    for day in range(5):
        for _ in range(3):
            life.record("听歌", character_id="fish", event_type="music",
                        occurred_at=evening + timedelta(days=day), now=save_at)
        for status in ("imagined", "planned", "wish"):
            life.record("读书", character_id="fish", event_type="reading", status=status,
                        occurred_at=evening + timedelta(days=day), now=save_at)
    assert life.habits("fish", now=evening + timedelta(days=1)) == ()
    [habit] = life.habits("fish", now=save_at)
    assert (habit.activity_type, habit.period, habit.matching_days, habit.observed_days) == ("music", "晚上", 5, 5)
    assert life.habits("big_fish", now=save_at) == ()


@pytest.mark.parametrize("profile,expected", [("A", ("reading", "晚上")), ("B", ("music", "早上")), ("C", None)])
def test_desktop_contact_and_chat_models_receive_the_same_habits(tmp_path, monkeypatch, profile, expected):
    runtime, life, model, conversation, attempts = setup_desktop(
        tmp_path, monkeypatch, outcome="friendly", with_discovery=False,
    )
    start = datetime.now(UTC).astimezone(SHANGHAI) - timedelta(days=15)
    seed_history(life, profile, start=start)
    with runtime.database.connection() as db:
        db.execute("UPDATE life_states SET mood='好奇' WHERE character_id='big_fish'")
    pipeline = DesktopContact(runtime, life, model)
    assert asyncio.run(pipeline.tick()) == "delivered"
    [signals] = attempts
    assert signals["candidate_type"] == "self_social"

    replies = {("reading", "晚上"): "晚上我常常想翻翻书。", ("music", "早上"): "早上我常常想听会儿歌。"}
    received = []

    class LocalChatModel:
        async def reply(self, messages):
            expression = next((msg["content"] for msg in messages if msg["content"].startswith("<expression_context")), None)
            if expression:
                payload = json.loads(next(line for line in expression.splitlines() if line.startswith("{")))
                received.extend(payload.get("habits", []))
            habit = received[0] if received else None
            key = (habit["activity_type"], habit["period"]) if habit else None
            return replies.get(key, "我喜欢每天都看看有什么新鲜事。")

    chat = ChatService(ConversationRepository(runtime.database), MessageRepository(runtime.database),
                       LocalChatModel(), "角色本人", life_repository=life)
    result = asyncio.run(chat.send(conversation["id"], "你平时有什么习惯？"))
    assert received == signals.get("habits", [])
    if expected:
        [habit] = received
        assert (habit["activity_type"], habit["period"], habit["status"]) == (*expected, "established")
        assert result["content"] == replies[expected]
    else:
        assert not received
        assert result["content"] == "我喜欢每天都看看有什么新鲜事。"
