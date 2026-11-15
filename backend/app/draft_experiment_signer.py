"""Offline, operator-confirmed signer for one B1b draft experiment grant."""

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path

from . import authorized_draft_experiment as lab
from .draft_experiment import ExperimentCase


_PLAN_MAX_BYTES = 8192
_ISSUANCE_WINDOW = timedelta(minutes=10)


def _load_plan(path: Path):
    path = Path(os.path.abspath(path))
    lab._check_path(path, required=True)
    try:
        with path.open("rb") as handle:
            raw = handle.read(_PLAN_MAX_BYTES + 1)
        if len(raw) > _PLAN_MAX_BYTES:
            raise lab.ExperimentRejected("invalid_experiment_plan")
        return json.loads(raw.decode("utf-8"), object_pairs_hook=lab._unique_keys)
    except (OSError, ValueError, RecursionError):
        raise lab.ExperimentRejected("invalid_experiment_plan") from None


def _validate_plan(plan, *, ledger, provider, now):
    if type(plan) is not dict:
        raise lab.ExperimentRejected("invalid_experiment_plan")
    try:
        run = lab._run_id(plan.get("run_id"))
        issued = lab._parse_time(plan.get("issued_at"))
        expires = lab._parse_time(plan.get("expires_at"))
        sample = lab._sample(ExperimentCase(plan.get("case")))
        expected = lab._claims(ledger, provider, run, sample, issued, expires)
        if (lab._canonical(plan) != lab._canonical(expected)
                or issued > now or expires <= now or expires - issued != _ISSUANCE_WINDOW):
            raise lab.ExperimentRejected("experiment_plan_binding_mismatch")
    except lab.ExperimentRejected:
        raise
    except (TypeError, ValueError, KeyError):
        raise lab.ExperimentRejected("invalid_experiment_plan") from None
    # This identity read is repeated inside the transaction that installs no files,
    # proving the signed plan belongs to the currently opened, constrained ledger.
    with ledger.atomic() as sql:
        row = sql.execute("SELECT ledger_id,audience FROM experiment_identity WHERE singleton=1").fetchone()
        if row is None or tuple(row) != (plan["ledger_id"], lab._REAL_AUDIENCE):
            raise lab.ExperimentRejected("experiment_ledger_identity_mismatch")
        if sql.execute("SELECT 1 FROM experiment_requests WHERE run_id=?", (run,)).fetchone():
            raise lab.ExperimentRejected("experiment_budget_consumed")
    return run, expected


def sign_plan_file(plan_path: Path, *, confirmed_run_id: str, key: bytes, now: datetime | None = None):
    """Validate against current trusted inputs, then atomically install one signed grant.

    This function is intentionally never invoked by planning or experiment execution.
    The command-line wrapper additionally requires a typed run ID confirmation.
    """
    root = lab._real_root()
    ledger = lab._Ledger(root, lab._REAL_AUDIENCE)
    provider = lab._configured_provider()
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    plan = _load_plan(Path(plan_path))
    run, claims = _validate_plan(plan, ledger=ledger, provider=provider, now=instant)
    if confirmed_run_id != run:
        raise lab.ExperimentRejected("operator_confirmation_mismatch")
    if not isinstance(key, bytes) or len(key) != 32:
        raise lab.ExperimentRejected("experiment_grant_key_unavailable")

    directory = root / "grants"
    directory.mkdir(mode=0o700, exist_ok=True)
    lab._check_path(directory, directory=True, required=True)
    target = directory / f"{run}.json"
    signed = {"claims": claims, "signature": lab.hmac.digest(key, lab._canonical(claims), "sha256").hex()}
    payload = lab._canonical(signed)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise lab.ExperimentRejected("experiment_grant_already_exists") from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise lab.ExperimentRejected("experiment_grant_install_failed") from None
    lab._check_path(target, required=True)
    return run


def sign_real_plan_from_environment(plan_path: Path, *, confirmed_run_id: str):
    """Production CLI entry; secrets are read only after plan and operator checks."""
    root = lab._real_root()
    ledger = lab._Ledger(root, lab._REAL_AUDIENCE)
    provider = lab._configured_provider()
    now = datetime.now(UTC)
    plan = _load_plan(Path(plan_path))
    run, _ = _validate_plan(plan, ledger=ledger, provider=provider, now=now)
    if confirmed_run_id != run:
        raise lab.ExperimentRejected("operator_confirmation_mismatch")
    key = lab._verification_key()
    return sign_plan_file(plan_path, confirmed_run_id=confirmed_run_id, key=key, now=now)
