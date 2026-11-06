"""Legacy fixtures are built from old migrations; original project DB is never opened."""

import json
import sqlite3

import pytest

import app.db as db_module
from app.db import Database
from app.contact import ContactService
from app.life import LifeRepository
from app.memory import MemoryRepository
from app.repositories import ConversationRepository

AT = "2026-01-01T03:00:00+00:00"


def legacy_database(path, monkeypatch, version=11):
    database = Database(str(path))
    with monkeypatch.context() as patch:
        patch.setattr(db_module, "MIGRATIONS", {k:v for k,v in db_module.MIGRATIONS.items() if k <= version})
        database.initialize()
    return database


def seed_legacy(db):
    with db.connection() as c:
        c.execute("CREATE TABLE legacy_conversation_ownership(conversation_id TEXT PRIMARY KEY, character_id TEXT NOT NULL, evidence TEXT NOT NULL)")
        c.execute("INSERT INTO legacy_conversation_ownership VALUES ('known','big_fish','explicit local operator confirmation for test fixture')")
        for conv in ("known", "unknown", "mixed"):
            c.execute("INSERT INTO conversations VALUES (?, ?, ?, ?)",(conv,conv,AT,AT))
            c.execute("INSERT INTO messages VALUES (?, ?, 'user', ?, ?)",(conv+'-user',conv,conv+' evidence',AT))
        for char in ("big_fish","legacy_other"):
            c.execute("INSERT INTO life_states(character_id,updated_at) VALUES (?,?)",(char,AT))
            c.execute("INSERT INTO life_events(id,character_id,event_type,status,occurred_at,content,source,generation_method,created_at,activity_status) VALUES (?,?,'exploring','completed',?,'shell','character','rule_engine',?,'completed')",(char+'-event',char,AT,AT))
            c.execute("INSERT INTO life_share_intents(id,character_id,event_id,status,reason,created_at) VALUES (?,?,?,'delivered','discovery',?)",(char+'-share',char,char+'-event',AT))
        for index,(conv,char) in enumerate((("known","big_fish"),("mixed","big_fish"),("mixed","legacy_other"))):
            mid, iid = f"proactive-{index}",f"intent-{index}"
            c.execute("INSERT INTO messages VALUES (?,?,'assistant','saved share',?)",(mid,conv,AT))
            c.execute("INSERT INTO contact_intents(id,character_id,source,source_id,event_id,share_intent_id,importance,reason,topic,status,created_at,expires_at) VALUES (?,?,'life_share',?,?,?,'normal','saved','shell','delivered',?,?)",(iid,char,str(index),char+'-event',char+'-share',AT,'2027-01-01'))
            c.execute("INSERT INTO proactive_messages(id,intent_id,attempt_no,content,status,created_at,conversation_id,chat_message_id,delivered_at) VALUES (?,?,0,'saved share','delivered',?,?,?,?)",(mid,iid,AT,conv,mid,AT))
        for name in ("known","unknown","mixed","orphan"):
            c.execute("INSERT INTO memory_items(id,memory_type,content,canonical_key,importance,confidence,status,created_at,updated_at) VALUES (?,'user_preference',?,?,0.8,0.9,'active',?,?)",(name+'-memory',name+' memory',name,AT,AT))
            if name!='orphan':
                c.execute("INSERT INTO memory_sources VALUES (?,?,?,'user','explicit_fact',NULL,?)",(name+'-source',name+'-memory',name+'-user',AT))
        c.execute("INSERT INTO memory_change_proposals(id,proposal_type,target_memory_id,proposed_memory_type,proposed_content,proposed_canonical_key,evidence_message_id,correction_intent,relation,confidence,evidence,status,created_at) VALUES ('proposal','correction','known-memory','user_preference','new','known','known-user','explicit_correction','conflict',0.9,'evidence','pending',?)",(AT,))
        context={"candidate":{"memory_type":"user_preference","content":"known evidence","canonical_key":"known"},"matched_memories":[{"id":"known-memory","content":"known memory","updated_at":AT}],"reason":"review","relation":"potential_conflict","evidence":["source"]}
        c.execute("INSERT INTO memory_correction_clarifications(id,idempotency_key,conversation_id,trigger_message_id,source_proposal_id,conflict_context,assistant_message_id,status,created_at,updated_at) VALUES ('clarification','legacy-clarification','known','known-user','proposal',?,'proactive-0','awaiting_user',?,?)",(json.dumps(context),AT,AT))
        c.execute("INSERT INTO memory_change_log(id,memory_id,action,actor_type,created_at,source_message_id) VALUES ('audit','known-memory','created','system_rule',?,'known-user')",(AT,))


def test_upgrade_v11_preserves_graph_and_quarantines_ambiguous_rows(tmp_path,monkeypatch):
    db=legacy_database(tmp_path/'v11.db',monkeypatch)
    seed_legacy(db)
    with db.connection() as c:
        before={t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('conversations','messages','memory_items','memory_sources','memory_change_proposals','memory_correction_clarifications','life_events','contact_intents','proactive_messages')}
    db.initialize();db.initialize()
    with db.connection() as c:
        assert c.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==16
        assert all(c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]==count for t,count in before.items())
        assert dict(c.execute('SELECT id,character_id FROM conversations').fetchall())=={'known':'big_fish','unknown':None,'mixed':None}
        assert dict(c.execute('SELECT id,character_id FROM memory_items').fetchall())=={'known-memory':'big_fish','unknown-memory':None,'mixed-memory':None,'orphan-memory':None}
        row=c.execute("SELECT * FROM memory_correction_clarifications WHERE id='clarification'").fetchone()
        assert row['character_id']=='big_fish' and row['source_proposal_id']=='proposal' and row['assistant_message_id']=='proactive-0'
        assert json.loads(row['conflict_context'])['matched_memories'][0]['id']=='known-memory'
        assert c.execute("SELECT character_id FROM memory_change_log WHERE id='audit'").fetchone()[0]=='big_fish'
        assert c.execute("SELECT owner_user_id FROM characters WHERE id='legacy_other'").fetchone()[0] is None
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert [x['id'] for x in ConversationRepository(db).list()]==['known']
    assert [x.id for x in MemoryRepository(db).list()]==['known-memory']


def test_migration12_failure_rolls_back_entire_schema_and_marker(tmp_path,monkeypatch):
    db=legacy_database(tmp_path/'rollback.db',monkeypatch);seed_legacy(db)
    with monkeypatch.context() as patch:
        patch.setitem(db_module.MIGRATIONS,12,db_module.MIGRATIONS[12]+'\nSELECT * FROM injected_missing_table;')
        with pytest.raises(sqlite3.OperationalError): db.initialize()
    with db.connection() as c:
        assert c.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==11
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone()
        assert 'character_id' not in {r[1] for r in c.execute('PRAGMA table_info(conversations)')}
        assert c.execute("SELECT source_proposal_id FROM memory_correction_clarifications WHERE id='clarification'").fetchone()[0]=='proposal'
    db.initialize()
    with db.connection() as c: assert c.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==16


def test_old_proactive_delivery_alone_does_not_prove_ownership(tmp_path,monkeypatch):
    db=legacy_database(tmp_path/'unconfirmed.db',monkeypatch);seed_legacy(db)
    with db.connection() as c: c.execute('DROP TABLE legacy_conversation_ownership')
    db.initialize()
    with db.connection() as c:
        assert c.execute('SELECT COUNT(*) FROM conversations WHERE character_id IS NULL').fetchone()[0]==3
        assert c.execute('SELECT COUNT(*) FROM memory_items WHERE character_id IS NULL').fetchone()[0]==4
        assert c.execute('SELECT COUNT(*) FROM contact_intents WHERE ownership_verified=0').fetchone()[0]==3
        assert c.execute('SELECT COUNT(*) FROM proactive_messages').fetchone()[0]==3
    assert ContactService(LifeRepository(db)).list_intents()==[]


def test_v1_upgrade_keeps_unattributable_messages_isolated(tmp_path,monkeypatch):
    db=legacy_database(tmp_path/'v1.db',monkeypatch,version=1)
    with db.connection() as c:
        c.execute("INSERT INTO conversations VALUES ('old','legacy',?,?)",(AT,AT))
        c.execute("INSERT INTO messages VALUES ('old-msg','old','user','keep me',?)",(AT,))
    db.initialize()
    with db.connection() as c:
        assert c.execute("SELECT content FROM messages WHERE id='old-msg'").fetchone()[0]=='keep me'
        assert c.execute("SELECT character_id FROM conversations WHERE id='old'").fetchone()[0] is None
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert ConversationRepository(db).list()==[]
