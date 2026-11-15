from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / 'backend'))
from app.contact import ContactService
from app.desktop_runtime import DesktopRuntime
from app.life import LifeRepository
from app.memory import EvidenceType, MemoryCreate, MemoryRepository, MemoryService, MemoryType, SourceRole
from app.ownership import character_scope
from app.repositories import ConversationRepository, MessageRepository

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
    else:
        message = MessageRepository(runtime.database).add(conversation['id'], 'user', '最近在听音乐')
        with runtime.database.connection() as db:
            db.execute('UPDATE messages SET created_at=? WHERE id=?', ((datetime.now(UTC) - timedelta(hours=25)).isoformat(), message['id']))
