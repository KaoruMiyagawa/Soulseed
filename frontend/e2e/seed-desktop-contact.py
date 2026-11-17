from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / 'backend'))
from app.contact import ContactService
from app.desktop_runtime import DesktopRuntime
from app.life import LifeRepository
from app.life_engine import ACTIVITIES, LifeEngine
from app.memory import EvidenceType, MemoryCreate, MemoryRepository, MemoryService, MemoryType, SourceRole
from app.ownership import character_scope
from app.repositories import ConversationRepository, MessageRepository


def seed_habit_scenario(life, scenario):
    now = datetime.now(UTC)
    activity = 'music' if scenario == 'morning' else 'reading'
    rival = 'reading' if activity == 'music' else 'music'
    hour, period = (9, '早上') if scenario == 'morning' else (20, '晚上')
    # Keep the Windows clock untouched. Pick an isolated character timezone
    # where the first real scheduler tick lands in the intended local period.
    offset = (hour - now.hour + 12) % 24 - 12
    zone_id = 'Etc/UTC' if offset == 0 else f'Etc/GMT{-offset:+}'
    life.set_timezone('big_fish', zone_id, now=now)
    local = now.astimezone(ZoneInfo(zone_id))
    engine = LifeEngine(life)
    sparse = scenario == 'sparse'
    for index in range(1 if sparse else 27):
        at = (local - timedelta(days=36 - index)).replace(hour=hour, minute=0)
        event = life.record(ACTIVITIES[activity][0], event_type=activity, feeling='开心', now=at)
        engine.learn_from_completion(event, at)
    for index, kind in enumerate(('tidying', 'cooking', 'resting')):
        at = (local - timedelta(days=5 - index)).replace(hour=14, minute=0)
        life.record(ACTIVITIES[kind][0], event_type=kind, now=at)

    slot = int(now.timestamp() // 5400)
    # At this slot the rival leads by 0.12 without a habit. The 0.24 bonus
    # can reverse the result, while the existing repeat penalty still wins.
    for kind in ACTIVITIES:
        engine.set_preference('big_fish', kind, 0.25, now=now)
    jitter_gap = engine._unit('big_fish', slot, activity) - engine._unit('big_fish', slot, rival)
    rival_weight = 1.55 + 0.12 + 0.22 * jitter_gap
    engine.set_preference('big_fish', activity, 1.55, now=now)
    engine.set_preference('big_fish', rival, rival_weight, now=now)
    expected = rival if scenario in {'repeat', 'sparse'} else activity
    if scenario == 'repeat':
        at = now - timedelta(minutes=10)
        life.record("刚刚阅读过", event_type=activity, now=at)
    plan = None
    if scenario == 'planned':
        plan = life.record("先整理小角落（验收计划）", event_type='tidying', status='planned',
                           planned_start_at=now - timedelta(minutes=1), now=now)
        expected = 'tidying'
    return {
        'scenario': scenario, 'activity': activity, 'period': period, 'slot': slot,
        'zone_id': zone_id, 'expected': expected, 'plan_id': plan['id'] if plan else None,
        'rival_weight': rival_weight, 'baseline_margin': round(rival_weight - 1.55 - 0.22 * jitter_gap, 3),
        'has_habit': not sparse,
    }


target = Path(sys.argv[1]).resolve()
assert target.is_relative_to((root / 'desktop-build' / 'qa').resolve())
target.mkdir(parents=True, exist_ok=True)
assert not (target / 'soulseed-desktop.db').exists()
with character_scope('big_fish'):
    runtime = DesktopRuntime(target, 'a' * 64)
    runtime.database.initialize()
    life = LifeRepository(runtime.database)
    conversation = ConversationRepository(runtime.database).create()
    ContactService(life).set_preferences(quiet_start_minute=0, quiet_end_minute=0)
    if len(sys.argv) > 2 and sys.argv[2] == 'self':
        source = MessageRepository(runtime.database).add(conversation['id'], 'user', '我很喜欢蓝色玻璃珠，想用它们串手链')
        with runtime.database.connection() as db:
            db.execute('UPDATE messages SET created_at=? WHERE id=?', ((datetime.now(UTC) - timedelta(days=100)).isoformat(), source['id']))
        memory = MemoryService(MemoryRepository(runtime.database)).create_memory(MemoryCreate(
            memory_type=MemoryType.USER_PREFERENCE,
            content='用户很喜欢蓝色玻璃珠，想用它们串手链',
            canonical_key='user:preference:blue-glass-beads', importance=0.9, confidence=0.95,
        ))
        MemoryService(MemoryRepository(runtime.database)).add_source(
            memory.id, source['id'], SourceRole.USER, EvidenceType.EXPLICIT_FACT,
            '我很喜欢蓝色玻璃珠，想用它们串手链',
        )
        with runtime.database.connection() as db:
            db.execute("UPDATE life_states SET mood='好奇' WHERE character_id='big_fish'")
        if len(sys.argv) > 4 and sys.argv[3] == 'habit':
            scenario = sys.argv[4]
            assert scenario in {'evening', 'morning', 'planned', 'repeat', 'sparse'}
            config = seed_habit_scenario(life, scenario)
            (target / 'habit-scenario.json').write_text(json.dumps(config), encoding='utf-8')
        elif len(sys.argv) > 3:
            activity = sys.argv[3]
            assert activity in {'music', 'reading'}
            engine = LifeEngine(life)
            today = datetime.now(UTC)
            for index in range(27):
                at = today - timedelta(days=35 - index)
                event = life.record(ACTIVITIES[activity][0], event_type=activity, feeling='开心', now=at)
                engine.learn_from_completion(event, at)
            # Match the recent activity history so the preference weight determines the choice.
            for index, kind in enumerate(('tidying', 'cooking', 'resting', 'exploring', 'daydreaming')):
                life.record(ACTIVITIES[kind][0], event_type=kind,
                            now=today - timedelta(days=7 - index))
            assert life.preference_growth()[0]['tendency'] == 'established'
    else:
        message = MessageRepository(runtime.database).add(conversation['id'], 'user', '最近在听音乐')
        with runtime.database.connection() as db:
            db.execute('UPDATE messages SET created_at=? WHERE id=?', ((datetime.now(UTC) - timedelta(hours=25)).isoformat(), message['id']))
