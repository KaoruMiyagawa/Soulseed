"""Interactively sign one checked B1b plan. This command does not run it."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.authorized_draft_experiment import ExperimentRejected
from app.draft_experiment_signer import sign_real_plan_from_environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="offline --plan-real JSON file")
    parser.add_argument("--confirm", required=True, metavar="RUN_ID",
                        help="explicitly confirm the run ID to be signed")
    args = parser.parse_args()
    try:
        typed = input(f"仅离线签发一次草稿授权。请键入完整 run_id {args.confirm}: ").strip()
        if typed != args.confirm:
            raise ExperimentRejected("operator_confirmation_mismatch")
        run = sign_real_plan_from_environment(args.plan, confirmed_run_id=typed)
        print(f"grant_signed run_id={run}; no model request was made")
        return 0
    except ExperimentRejected as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError):
        print("experiment_grant_signing_failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
