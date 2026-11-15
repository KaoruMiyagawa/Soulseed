"""Explicit, operator-run Legacy-to-SQLite model budget cutover for Development."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import DEVELOPMENT_DATABASE_PATH
from app.db import Database, MIGRATIONS
from app.model_budget import ModelBudgetService
from app.model_budget_history import LegacyIdentity, ModelBudgetCutover


def _inventory(path: Path) -> dict[str, LegacyIdentity]:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("inventory_too_large")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_inventory_key")
            result[key] = value
        return result

    raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    if not isinstance(raw, dict) or len(raw) > 100_000:
        raise ValueError("inventory_must_be_a_bounded_object")
    result = {}
    for request_id, identity in raw.items():
        if not isinstance(identity, dict) or set(identity) != {"character_id", "source"}:
            raise ValueError("inventory_identity_shape_invalid")
        result[request_id] = LegacyIdentity(identity["character_id"], identity["source"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development-database", required=True, type=Path)
    parser.add_argument("--confirm-development-database", required=True, type=Path)
    parser.add_argument("--database-backup", required=True, type=Path)
    parser.add_argument("--general-log", required=True, type=Path)
    parser.add_argument("--exploration-log", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--confirm-no-legacy-writers", action="store_true",
                        help="confirm all old service processes and in-flight requests have stopped")
    args = parser.parse_args()
    expected = DEVELOPMENT_DATABASE_PATH.resolve(strict=False)
    if (args.development_database.resolve(strict=False) != expected
            or args.confirm_development_database.resolve(strict=False) != expected):
        parser.error("the selected database and its confirmation must both be the fixed Development database")
    if not args.confirm_no_legacy_writers:
        parser.error("stop and independently verify all old Development model writers first")

    inventory = _inventory(args.inventory)
    if not expected.is_file() or Database._readonly_schema_version(expected) != max(MIGRATIONS):
        parser.error("Development database must already pass integrity and be at the current schema version")
    backup = args.database_backup.expanduser().resolve(strict=True)
    project_root = Path(__file__).resolve().parents[2]
    try:
        backup.relative_to(project_root)
    except ValueError:
        pass
    else:
        parser.error("database backup must be outside the repository")
    if (not backup.is_file() or backup.samefile(expected)
            or Database._readonly_schema_version(backup) != max(MIGRATIONS)
            or Database._sha256(backup) != Database._sha256(expected)
            or any(Path(str(path) + suffix).exists() for path in (expected, backup)
                   for suffix in ("-wal", "-shm", "-journal"))):
        parser.error("provide a stopped, byte-identical, independently stored current-schema database backup")
    database = Database(str(expected), environment="development", allow_protected_database_access=True,
                        protected_database_confirmation=str(expected))
    database.initialize()
    budget = ModelBudgetService(database, global_limit=28,
                                source_limits={"chat": 20, "contact": 20, "exploration": 8})
    cutover = ModelBudgetCutover(
        budget,
        files={"general": args.general_log, "exploration": args.exploration_log},
        inventory=inventory,
    )
    cutover.begin_drain()
    cutover.seal()
    report = cutover.import_history()
    print(json.dumps({
        "gate_eligible": report.gate_eligible,
        "inserted_requests": report.inserted_requests,
        "unique_requests": report.unique_requests,
        "unknown_outcomes": report.unknown_outcomes,
        "failure_reason": report.failure_reason,
    }, ensure_ascii=True, sort_keys=True))
    return 0 if report.gate_eligible else 2


if __name__ == "__main__":
    raise SystemExit(main())
