"""Offline budget kernel. No model, JSONL import, scheduler, or network access."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from .db import Database
from .ownership import current_character


class BudgetRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class BudgetResult:
    request_id: str
    status: str
    changed: bool


class ModelBudgetService:
    """Host-owned limits; None means unlimited, never an implicit default cap.

    Sub-limits apply per character and source. Global usage spans all characters
    and sources. A policy is frozen per UTC day to reject inconsistent workers.
    """

    def __init__(self, database: Database, *, global_limit=None, source_limits=None,
                 clock=lambda: datetime.now(UTC), lease=timedelta(minutes=5)):
        limits = dict(source_limits or {})
        if (global_limit is not None and (type(global_limit) is not int or global_limit < 1)
                or any(not isinstance(k, str) or not k or type(v) is not int or v < 1 for k, v in limits.items())
                or lease <= timedelta(0)):
            raise ValueError('invalid_budget_policy')
        self.database, self.clock, self.lease = database, clock, lease
        self.global_limit, self.source_limits = global_limit, MappingProxyType(limits)
        self.policy = json.dumps([global_limit, limits], sort_keys=True, separators=(',', ':'))

    def _now(self):
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError('aware_clock_required')
        return value.astimezone(UTC)

    def _window(self, db, day):
        db.execute('INSERT OR IGNORE INTO model_budget_windows(day,policy_json) VALUES(?,?)', (day, self.policy))
        row = db.execute('SELECT * FROM model_budget_windows WHERE day=?', (day,)).fetchone()
        if row['policy_json'] != self.policy:
            raise BudgetRejected('budget_policy_conflict')
        return row

    def set_compatibility_gate(self, day: str, *, ready: bool):
        """Trusted cutover hook, not proof of historical import; default is closed.

        Future integration must establish history completeness before calling.
        Never exposed as an end-user API. This phase calls it only in tests.
        """
        if datetime.strptime(day, '%Y-%m-%d').date().isoformat() != day or type(ready) is not bool:
            raise ValueError('invalid_window_gate')
        with self.database.atomic() as db:
            self._window(db, day)
            db.execute('UPDATE model_budget_windows SET history_ready=? WHERE day=?', (int(ready), day))

    @staticmethod
    def _scope(character_id):
        if not character_id or character_id != current_character.get():
            raise BudgetRejected('character_scope_mismatch')

    def reserve(self, request_id: str, character_id: str, source: str, *, autonomous_reservation_id=None):
        self._scope(character_id)
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128 or not isinstance(source, str) or not 1 <= len(source) <= 64:
            raise ValueError('invalid_request_identity')
        now = self._now()
        day, at = now.date().isoformat(), now.isoformat()
        with self.database.atomic() as db:
            old = db.execute('SELECT * FROM model_budget_requests WHERE id=?', (request_id,)).fetchone()
            if old:
                if (old['character_id'], old['source'], old['autonomous_reservation_id']) != (character_id, source, autonomous_reservation_id):
                    raise BudgetRejected('request_identity_conflict')
                return BudgetResult(request_id, old['status'], False)
            window = self._window(db, day)
            if not window['history_ready']:
                raise BudgetRejected('history_not_confirmed')
            if not db.execute("SELECT 1 FROM characters WHERE id=? AND ownership_status='owned'", (character_id,)).fetchone():
                raise BudgetRejected('character_not_owned')
            if autonomous_reservation_id and not db.execute("SELECT 1 FROM autonomous_contact_reservations WHERE id=? AND character_id=? AND request_key=? AND request_status='reserved'", (autonomous_reservation_id, character_id, request_id)).fetchone():
                raise BudgetRejected('autonomous_reservation_mismatch')
            used = db.execute("SELECT COUNT(*) FROM model_budget_requests WHERE day=? AND status<>'released'", (day,)).fetchone()[0]
            if self.global_limit is not None and used >= self.global_limit:
                raise BudgetRejected('global_budget_exhausted')
            sub_used = db.execute("SELECT COUNT(*) FROM model_budget_requests WHERE day=? AND character_id=? AND source=? AND status<>'released'", (day, character_id, source)).fetchone()[0]
            if source in self.source_limits and sub_used >= self.source_limits[source]:
                raise BudgetRejected('source_budget_exhausted')
            db.execute("INSERT INTO model_budget_requests VALUES(?,?,?,?,'reserved',?,?,?,?)", (request_id, character_id, source, day, at, at, (now+self.lease).isoformat(), autonomous_reservation_id))
            return BudgetResult(request_id, 'reserved', True)

    def _transition(self, request_id, character_id, target):
        self._scope(character_id)
        now = self._now()
        with self.database.atomic() as db:
            row = db.execute('SELECT * FROM model_budget_requests WHERE id=? AND character_id=?', (request_id, character_id)).fetchone()
            if not row:
                raise BudgetRejected('request_missing')
            if row['status'] == target:
                return BudgetResult(request_id, target, False)
            expected = 'reserved' if target in {'started', 'released'} else 'started'
            if row['status'] != expected:
                raise BudgetRejected('invalid_request_transition')
            if target == 'started':
                if row['day'] != now.date().isoformat() or row['lease_expires_at'] <= now.isoformat():
                    raise BudgetRejected('reservation_expired')
                if not self._window(db, row['day'])['history_ready']:
                    raise BudgetRejected('history_not_confirmed')
                if not db.execute("SELECT 1 FROM characters WHERE id=? AND ownership_status='owned'", (character_id,)).fetchone():
                    raise BudgetRejected('character_not_owned')
            db.execute('UPDATE model_budget_requests SET status=?,updated_at=?,lease_expires_at=? WHERE id=?', (target, now.isoformat(), (now+self.lease).isoformat(), request_id))
            return BudgetResult(request_id, target, True)

    def mark_started(self, request_id, character_id):
        """Only changed=True authorizes the first transport attempt. Never retry it."""
        return self._transition(request_id, character_id, 'started')

    def complete(self, request_id, character_id, *, success=True):
        if type(success) is not bool:
            raise ValueError('invalid_completion')
        return self._transition(request_id, character_id, 'completed' if success else 'failed')

    def mark_unknown(self, request_id, character_id):
        return self._transition(request_id, character_id, 'unknown')

    def release_unstarted(self, request_id, character_id):
        return self._transition(request_id, character_id, 'released')

    def usage_snapshot(self, character_id, *, day=None):
        self._scope(character_id)
        day = day or self._now().date().isoformat()
        with self.database.connection() as db:
            rows = db.execute('SELECT character_id,source,status FROM model_budget_requests WHERE day=?', (day,)).fetchall()
            gate = db.execute('SELECT history_ready FROM model_budget_windows WHERE day=?', (day,)).fetchone()
        charged = [r for r in rows if r['status'] != 'released']
        return {'day': day, 'global_used': len(charged), 'global_limit': self.global_limit,
                'history_ready': bool(gate and gate[0]),
                'source_used': {source: sum(r['character_id']==character_id and r['source']==source for r in charged) for source in set(self.source_limits) | {r['source'] for r in charged if r['character_id']==character_id}}}

    def recover(self, character_id):
        """Expired reserved requests are fenced and released; started stay charged."""
        self._scope(character_id)
        at = self._now().isoformat()
        with self.database.atomic() as db:
            rows = db.execute("SELECT id,status FROM model_budget_requests WHERE character_id=? AND status IN ('reserved','started') AND lease_expires_at<=?", (character_id, at)).fetchall()
            for row in rows:
                db.execute('UPDATE model_budget_requests SET status=?,updated_at=? WHERE id=?', ('released' if row['status']=='reserved' else 'unknown', at, row['id']))
            return len(rows)
