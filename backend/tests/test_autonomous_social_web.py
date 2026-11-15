import asyncio
import json
from datetime import UTC, datetime
import httpx
from app.autonomous_social import AutonomousContact
from app.config import get_settings
from app.contact import ContactService
from app.db import Database
from app.life import LifeRepository
from app.main import WebContactRuntime
from app.model_adapter import OpenAICompatibleAdapter
from app.model_budget import ModelBudgetService
from app.ownership import character_scope
from app.repositories import ConversationRepository


def test_web_strict_budget_joint_lifecycle(tmp_path, seed_budget_gate):
    db = Database(str(tmp_path / 'web-social.db'))
    db.initialize()
    life = LifeRepository(db)
    with character_scope('big_fish'):
        life.ensure_state('big_fish')
        ContactService(life).set_preferences(quiet_start_minute=0, quiet_end_minute=0)
        conversation = ConversationRepository(db).create()
        with db.connection() as sql:
            sql.execute("UPDATE life_states SET mood='好奇' WHERE character_id='big_fish'")
        budget = ModelBudgetService(db, global_limit=2)
        seed_budget_gate(budget, datetime.now(UTC).date().isoformat())
        settings = get_settings().model_copy(update={'model_enabled': True, 'model_api_key': 'test-only-key',
                                                       'contact_enabled': True, 'model_trust_env': False})
        calls = []
        def respond(request):
            signals = json.loads(json.loads(request.content)['messages'][-1]['content'])
            calls.append(signals)
            plan = {'decision': 'contact', 'choice': signals['choices'][0]['id'], 'style': 'curious',
                    'message': '忽然想来和你打个招呼。', 'evidence_ids': [], 'claims': []}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(plan)}, 'finish_reason': 'stop'}]})
        model = OpenAICompatibleAdapter(settings, strict_budget=True, budget_service=budget,
                                        budget_transport=httpx.MockTransport(respond))
        core = AutonomousContact(WebContactRuntime(db, settings, model), life, model)
        assert asyncio.run(core.tick()) == 'delivered'
        with db.connection() as sql:
            assert sql.execute("SELECT status FROM model_budget_requests").fetchone()[0] == 'completed'
            assert sql.execute("SELECT request_status,message_status FROM autonomous_contact_reservations").fetchone()[:] == ('settled','committed')
            assert sql.execute("SELECT conversation_id FROM proactive_messages").fetchone()[0] == conversation['id']
        assert len(calls) == 1
