"""Isolated cutover for cooperating Legacy writers and synthetic history only."""

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
import json
from os import fstat
from pathlib import Path
import sqlite3
from uuid import uuid4

from .config import DEVELOPMENT_DATABASE_PATH, validate_test_database_path
from .model_budget import BudgetRejected, ModelBudgetService, _CUTOVER_AUTHORITY


_FILE_KINDS = {"general", "exploration"}
_EVENTS = {"started", "http_started", "finished", "action_validation"}
_UNKNOWN_RESULTS = {"network_error", "timeout_connect", "timeout_read", "timeout_write",
                    "timeout_pool", "timeout_deadline"}
_MAX_FILE_BYTES = 16 * 1024 * 1024


class CutoverRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class LegacyIdentity:
    character_id: str
    source: str


@dataclass
class ImportReport:
    processed_records: int = 0
    unique_requests: int = 0
    duplicate_records: int = 0
    unconfirmed_records: int = 0
    unknown_outcomes: int = 0
    inserted_requests: int = 0
    failure_reason: str | None = None
    gate_eligible: bool = False


@dataclass
class _Request:
    file_kind: str
    identity: LegacyIdentity
    day: str
    started_at: datetime
    run_step: tuple
    events: dict = field(default_factory=dict)
    status: str = "unknown"

    @property
    def updated_at(self):
        return max(signature[0] for signature in self.events.values())


def _timestamp(value):
    if not isinstance(value, str):
        raise CutoverRejected("timestamp_missing")
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is not None:
            return stamp.astimezone(UTC)
    except (ValueError, OverflowError):
        pass
    raise CutoverRejected("timestamp_invalid")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CutoverRejected("duplicate_json_key")
        result[key] = value
    return result


def _result_status(value):
    if not isinstance(value, str):
        raise CutoverRejected("result_status_missing")
    if value in _UNKNOWN_RESULTS:
        return "unknown"
    if len(value) == 8 and value.startswith("http_") and value[5:].isascii() and value[5:].isdigit():
        code = int(value[5:])
        if 100 <= code <= 599:
            return "completed" if 200 <= code <= 299 else "failed"
    raise CutoverRejected("result_status_unrecognized")


class ModelBudgetCutover:
    """Host-owned simulator. Every synthetic writer must hold an admission.

    The inventory is an independent enumeration of all Legacy reservations,
    including identity evidence absent from the old JSONL format.
    """

    def __init__(self, budget: ModelBudgetService, *, files: Mapping[str, Path],
                 inventory: Mapping[str, LegacyIdentity]):
        if budget.database.environment not in ("testing", "development"):
            raise CutoverRejected("cutover_requires_testing_or_development_database")
        self.budget = budget
        self.database = budget.database
        if set(files) != _FILE_KINDS:
            raise CutoverRejected("required_log_inventory_missing")
        self._files = dict(files)
        if budget.database.environment == "development":
            # A live cutover must use an explicitly approved Development database
            # and external, configured ledgers. Do not let this coordinator point
            # at arbitrary files inside the repository.
            database_path = Path(budget.database.path).expanduser().resolve(strict=False)
            canonical_development_database = DEVELOPMENT_DATABASE_PATH.resolve(strict=False)
            confirmation = budget.database.protected_database_confirmation
            try:
                confirmed = Path(confirmation).expanduser().resolve(strict=False) if confirmation else None
            except (OSError, RuntimeError):
                confirmed = None
            if (database_path != canonical_development_database
                    and (not budget.database.allow_protected_database_access or confirmed != database_path)):
                raise CutoverRejected("development_database_approval_required")
            project_root = Path(__file__).resolve().parents[2]
            for configured in self._files.values():
                candidate = Path(configured).expanduser().resolve(strict=False)
                try:
                    candidate.relative_to(project_root)
                except ValueError:
                    pass
                else:
                    raise CutoverRejected("development_history_must_be_outside_repository")
                if candidate == database_path or candidate.suffix.lower() != ".jsonl":
                    raise CutoverRejected("invalid_development_history_path")
        self._inventory = dict(inventory)
        for request_id, identity in self._inventory.items():
            if (not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128
                    or type(identity) is not LegacyIdentity or not isinstance(identity.character_id, str)
                    or not identity.character_id.strip() or identity.source not in ("chat", "contact", "exploration")):
                raise CutoverRejected("invalid_request_inventory")
        self._inventory_digest = sha256(json.dumps(
            sorted((key, value.character_id, value.source) for key, value in self._inventory.items()),
            separators=(",", ":"),
        ).encode()).hexdigest()

    def _standalone(self):
        if self.database.in_transaction:
            raise CutoverRejected("cutover_requires_standalone_transaction")

    @staticmethod
    def _state(sql):
        return sql.execute("SELECT * FROM model_budget_cutover WHERE singleton=1").fetchone()

    @contextmanager
    def legacy_request(self):
        """Hold admission through final audit writes and closing old handles."""
        self._standalone()
        token = uuid4().hex
        with self.database.atomic() as sql:
            if self._state(sql)["phase"] != "legacy":
                raise CutoverRejected("legacy_admission_closed")
            sql.execute("INSERT INTO model_budget_legacy_inflight VALUES(?)", (token,))
        try:
            yield
        finally:
            with self.database.atomic() as sql:
                sql.execute("DELETE FROM model_budget_legacy_inflight WHERE token=?", (token,))

    def begin_drain(self):
        self._standalone()
        with self.database.atomic() as sql:
            if self._state(sql)["phase"] not in ("legacy", "draining"):
                raise CutoverRejected("legacy_rollback_forbidden")
            sql.execute("UPDATE model_budget_cutover SET phase='draining' WHERE singleton=1")

    def _read_snapshot(self):
        files = {}
        contents = {}
        identities = set()
        for kind, configured in sorted(self._files.items()):
            try:
                if self.database.environment == "testing":
                    path = validate_test_database_path(configured, self.database.test_data_root)
                else:
                    path = Path(configured).expanduser().resolve(strict=False)
                    if path.suffix.lower() != ".jsonl":
                        raise CutoverRejected("invalid_development_history_path")
                if path.suffix.lower() != ".jsonl":
                    raise CutoverRejected("invalid_history_file_type")
                with path.open("rb") as handle:
                    metadata = fstat(handle.fileno())
                    identity = (metadata.st_dev, metadata.st_ino)
                    if identity in identities:
                        raise CutoverRejected("duplicate_history_file")
                    identities.add(identity)
                    data = handle.read(_MAX_FILE_BYTES + 1)
            except FileNotFoundError as exc:
                raise CutoverRejected(f"required_log_missing:{kind}") from exc
            except (OSError, ValueError) as exc:
                raise CutoverRejected(f"history_file_unavailable:{kind}") from exc
            if len(data) > _MAX_FILE_BYTES:
                raise CutoverRejected(f"history_file_too_large:{kind}")
            relative_path = (str(path.relative_to(Path(self.database.test_data_root).resolve()))
                             if self.database.environment == "testing" else str(path))
            files[kind] = {"path": relative_path,
                           "bytes": len(data), "sha256": sha256(data).hexdigest()}
            contents[kind] = data
        return files, contents

    def seal(self):
        """Freeze a manifest after the last admitted writer closes its handles."""
        self._standalone()
        with self.database.atomic() as sql:
            state = self._state(sql)
            if state["phase"] != "draining":
                raise CutoverRejected("drain_required")
            if sql.execute("SELECT 1 FROM model_budget_legacy_inflight LIMIT 1").fetchone():
                raise CutoverRejected("legacy_writers_still_active")
            files, _ = self._read_snapshot()
            now = self.budget._now()
            manifest = {"files": files, "cutoff": now.isoformat(), "inventory": self._inventory_digest}
            sql.execute("UPDATE model_budget_cutover SET phase='sealed',manifest_json=?,utc_day=? WHERE singleton=1",
                        (json.dumps(manifest, sort_keys=True), now.date().isoformat()))

    def _verify_manifest(self, state):
        manifest = json.loads(state["manifest_json"])
        if manifest["inventory"] != self._inventory_digest:
            raise CutoverRejected("request_inventory_changed")
        files, contents = self._read_snapshot()
        if manifest["files"] != files:
            raise CutoverRejected("sealed_history_changed")
        return contents, _timestamp(manifest["cutoff"])

    def _parse(self, contents, cutoff, report):
        requests = {}
        for kind, data in contents.items():
            if data and not data.endswith(b"\n"):
                raise CutoverRejected(f"truncated_jsonl:{kind}")
            for line_number, line in enumerate(data.splitlines(), 1):
                report.processed_records += 1
                try:
                    row = json.loads(line.decode("utf-8"), object_pairs_hook=_json_object)
                    if self._merge(kind, row, cutoff, requests):
                        report.duplicate_records += 1
                    report.unique_requests = len(requests)
                except (ValueError, RecursionError) as exc:
                    raise CutoverRejected(f"malformed_jsonl:{kind}:{line_number}") from exc
                except CutoverRejected as exc:
                    raise CutoverRejected(f"{exc}:{kind}:{line_number}") from exc
        if self._inventory.keys() != requests.keys():
            report.unconfirmed_records = len(self._inventory.keys() - requests.keys())
            raise CutoverRejected("history_inventory_incomplete")
        report.unknown_outcomes = sum(request.status == "unknown" for request in requests.values())
        return requests

    def _merge(self, kind, row, cutoff, requests):
        if not isinstance(row, dict) or not isinstance(row.get("event"), str) or row["event"] not in _EVENTS:
            raise CutoverRejected("unrecognized_history_event")
        if row.get("budget_mode") not in (None, "legacy"):
            raise CutoverRejected("non_legacy_history_event")
        event, request_id = row["event"], row.get("id")
        identity = self._inventory.get(request_id) if isinstance(request_id, str) else None
        if identity is None:
            raise CutoverRejected("request_identity_unconfirmed")
        if (kind == "exploration") != (identity.source == "exploration"):
            raise CutoverRejected("request_source_conflict")
        if row.get("character_id", identity.character_id) != identity.character_id or row.get("source", identity.source) != identity.source:
            raise CutoverRejected("request_identity_conflict")
        at = _timestamp(row.get("at"))
        if at > cutoff:
            raise CutoverRejected("event_after_cutoff")
        run_step = row.get("exploration_run_id"), row.get("exploration_step")
        if kind == "exploration":
            run_id, step = run_step
            if not isinstance(run_id, str) or not run_id.strip() or type(step) is not int or step < 1:
                raise CutoverRejected("exploration_identity_missing")
        elif run_step != (None, None) or event in ("http_started", "action_validation"):
            raise CutoverRejected("request_source_conflict")
        day = at.date().isoformat()
        if event == "started" and row.get("day") != day:
            raise CutoverRejected("reservation_day_mismatch")
        status = _result_status(row.get("status")) if event == "finished" else None
        request = requests.get(request_id)
        if request is None:
            if event != "started":
                raise CutoverRejected("orphaned_history_event")
            request = requests[request_id] = _Request(kind, identity, day, at, run_step)
        if (request.file_kind, request.identity, request.run_step) != (kind, identity, run_step):
            raise CutoverRejected("request_identity_conflict")
        if "day" in row and row["day"] != request.day:
            raise CutoverRejected("reservation_day_mismatch")
        signature = (at, row.get("status") if event == "finished" else None)
        if event in request.events:
            if request.events[event] != signature:
                raise CutoverRejected("request_event_conflict")
            return True
        if request.events and (at < request.updated_at or event == "http_started" and "finished" in request.events):
            raise CutoverRejected("request_event_out_of_order")
        request.events[event] = signature
        if status is not None:
            request.status = status
        return False

    def _store_requests(self, sql, requests, *, already_imported):
        if not already_imported and sql.execute("SELECT 1 FROM model_budget_requests LIMIT 1").fetchone():
            raise CutoverRejected("preexisting_budget_requests")
        for request_id, request in requests.items():
            self.budget._window(sql, request.day)
            identity = request.identity
            values = (identity.character_id, identity.source, request.day, request.status,
                      request.started_at.isoformat(), request.updated_at.isoformat())
            if already_imported:
                row = sql.execute("SELECT character_id,source,day,status,created_at,updated_at FROM model_budget_requests WHERE id=?",
                                  (request_id,)).fetchone()
                if row is None or tuple(row) != values:
                    raise CutoverRejected("imported_ledger_mismatch")
            else:
                if not sql.execute("SELECT 1 FROM characters WHERE id=? AND ownership_status='owned'",
                                   (identity.character_id,)).fetchone():
                    raise CutoverRejected("history_character_unconfirmed")
                sql.execute("INSERT INTO model_budget_requests(id,character_id,source,day,status,created_at,updated_at,lease_expires_at) VALUES(?,?,?,?,?,?,?,?)",
                            (request_id, *values, values[-1]))

    def _verify_and_store(self, sql, state, report):
        phase, day = state["phase"], state["utc_day"]
        if sql.execute("SELECT 1 FROM model_budget_legacy_inflight LIMIT 1").fetchone():
            raise CutoverRejected("legacy_writers_still_active")
        if day != self.budget._now().date().isoformat():
            raise CutoverRejected("cutover_day_changed")
        contents, cutoff = self._verify_manifest(state)
        requests = self._parse(contents, cutoff, report)
        self._store_requests(sql, requests, already_imported=phase == "strict")
        self._verify_manifest(state)
        if day != self.budget._now().date().isoformat():
            raise CutoverRejected("cutover_day_changed")
        sql.execute("UPDATE model_budget_cutover SET phase='strict' WHERE singleton=1")
        self.budget.set_compatibility_gate(day, ready=True, _authority=_CUTOVER_AUTHORITY)
        return 0 if phase == "strict" else len(requests)

    @staticmethod
    def _failed_report(report, exc):
        report.failure_reason = "history_transaction_failed" if isinstance(exc, sqlite3.Error) else str(exc)
        report.unconfirmed_records = max(report.unconfirmed_records, 1)
        return report

    def import_history(self) -> ImportReport:
        """Verify the seal, import and open the current UTC day in one commit."""
        self._standalone()
        report = ImportReport()
        try:
            with self.database.atomic() as sql:
                state = self._state(sql)
                if state["phase"] not in ("sealed", "strict"):
                    raise CutoverRejected("verified_seal_required")
                sql.execute("SAVEPOINT verify_history")
                try:
                    inserted = self._verify_and_store(sql, state, report)
                except (CutoverRejected, BudgetRejected, sqlite3.Error) as exc:
                    sql.execute("ROLLBACK TO verify_history")
                    if state["phase"] == "strict":
                        self.budget.set_compatibility_gate(state["utc_day"], ready=False, _authority=_CUTOVER_AUTHORITY)
                        sql.execute("UPDATE model_budget_cutover SET phase='invalidated' WHERE singleton=1")
                    return self._failed_report(report, exc)
                finally:
                    sql.execute("RELEASE verify_history")
        except (CutoverRejected, BudgetRejected, sqlite3.Error) as exc:
            return self._failed_report(report, exc)
        report.inserted_requests = inserted
        report.gate_eligible = True
        return report
