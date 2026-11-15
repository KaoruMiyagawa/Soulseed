from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / 'backend'))
from app.contact import ContactService
from app.desktop_runtime import DesktopRuntime
from app.life import LifeRepository
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
    message = MessageRepository(runtime.database).add(conversation['id'], 'user', '最近在听音乐')
    with runtime.database.connection() as db:
        db.execute('UPDATE messages SET created_at=? WHERE id=?', ((datetime.now(UTC) - timedelta(hours=25)).isoformat(), message['id']))
