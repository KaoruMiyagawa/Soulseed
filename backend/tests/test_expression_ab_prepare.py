import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from scripts import expression_ab_eval as ab
from app.contact import ContactService
from app.life import LifeRepository


def test_ab_prompts_use_the_same_persona_and_fictional_character_data(tmp_path):
    db = ab.new_test_database(tmp_path, "fictional.db")
    ab.seed_fictional_data(db)
    persona = ab.resolve_system_prompt(None)
    old, new = ab.chat_prompts(db, "今天吃了什么？", [], persona)
    assert old[0] == new[0] == {"role": "system", "content": persona}
    assert old[-1] == new[-1] == {"role": "user", "content": "今天吃了什么？"}
    assert "吃了烤鱼" not in json.dumps(new, ensure_ascii=False)
    encoded = "{" + new[1]["content"].split("\n{", 1)[1].split("\n</expression_context>", 1)[0]
    data = json.loads(encoded)
    assert any(item["content"] == "想吃烤鱼" and item["status"] == "wish" for item in data["evidence"])
    assert not any(item["kind"] == "meal" and item["status"] == "completed" for item in data["evidence"])


def test_unrelated_emotional_prompt_has_no_ab_context_difference(tmp_path):
    db = ab.new_test_database(tmp_path, "fictional.db")
    ab.seed_fictional_data(db)
    old, new = ab.chat_prompts(db, "我想你了", [], ab.resolve_system_prompt(None))
    assert old == new


def test_contact_decision_blocks_model_when_user_declines(tmp_path):
    db = ab.new_test_database(tmp_path, "fictional.db")
    ab.seed_fictional_data(db)
    adapter = ab.DryCaptureModel()
    service = ab.prepare_contact_branch(db, tmp_path, "new", adapter, ab.resolve_system_prompt(None))
    service.set_preferences(allowed=False, now=ab.FIXED_NOW)
    assert asyncio.run(service.tick(now=ab.FIXED_NOW)) is None
    assert adapter.messages is None
    assert service.inbox(now=ab.FIXED_NOW) == []


def test_dry_run_ignores_unrelated_dotenv_and_requires_paid_flag(tmp_path):
    fake_formal = tmp_path / "fictional-formal.db"
    (tmp_path / ".env").write_text(f"SOULSEED_DATABASE_PATH={fake_formal}\nSOULSEED_MODEL_API_KEY=fake-test-key\n", encoding="utf-8")
    script = Path(ab.__file__).resolve()
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("SOULSEED_")}
    dry = subprocess.run([sys.executable, str(script)], cwd=tmp_path, env=environment,
                         capture_output=True, text=True, timeout=60)
    assert dry.returncode == 0
    assert "paid_requests=0" in dry.stdout
    assert not fake_formal.exists()
    refused = subprocess.run([sys.executable, str(script), "--execute"], cwd=tmp_path, env=environment,
                             capture_output=True, text=True, timeout=10)
    assert refused.returncode != 0
    assert "--paid-requests-approved" in refused.stderr
    assert not fake_formal.exists()
