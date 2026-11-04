"""Run backend tests or a local API only with an ephemeral testing database."""

from __future__ import annotations

import argparse
import os
import secrets
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"


def isolated_environment(test_root: Path, port: int) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("SOULSEED_")
        and key.upper() not in {"OPENAI_API_KEY", "SILICONFLOW_API_KEY", "AZURE_OPENAI_API_KEY"}
    }
    env.update({
        "SOULSEED_ENVIRONMENT": "testing",
        "SOULSEED_TEST_DATA_ROOT": str(test_root.resolve()),
        "SOULSEED_DATABASE_PATH": str((test_root / "soulseed-test.db").resolve()),
        "SOULSEED_LOCAL_PAIRING_SECRET": secrets.token_urlsafe(32),
        "SOULSEED_HOST": "127.0.0.1",
        "SOULSEED_PORT": str(port),
        "SOULSEED_MODEL_BASE_URL": "http://127.0.0.1:9/v1",
        "SOULSEED_MODEL_NAME": "local-test-only",
        "SOULSEED_MODEL_API_KEY": "",
        "SOULSEED_LIFE_BACKGROUND_ENABLED": "false",
        "SOULSEED_CONTACT_BACKGROUND_ENABLED": "false",
    })
    return env


def run_child(command: list[str], *, env: dict[str, str], cwd: Path, duration: float | None = None) -> int:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" and duration is not None else 0
    child = subprocess.Popen(command, cwd=cwd, env=env, creationflags=flags)
    try:
        if duration is not None:
            try:
                child.wait(timeout=duration)
                return child.returncode
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    child.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                return 0
        return child.wait()
    except KeyboardInterrupt:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        return 130


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    tests = subparsers.add_parser("tests", help="run pytest with a fresh isolated database")
    tests.add_argument("pytest_args", nargs=argparse.REMAINDER)
    api = subparsers.add_parser("api", help="run a local API against a disposable database")
    api.add_argument("--port", type=int, default=18081)
    api.add_argument("--duration", type=float, help=argparse.SUPPRESS)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="soulseed-testing-") as name:
        test_root = Path(name).resolve(strict=True)
        env = isolated_environment(test_root, 18081 if args.mode == "tests" else args.port)
        print("Soulseed environment=testing database=<test-root>/soulseed-test.db", flush=True)
        if args.mode == "tests":
            base_temp = test_root / "pytest-tmp"
            command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--basetemp", str(base_temp), *args.pytest_args]
            return run_child(command, env=env, cwd=BACKEND_ROOT)

        command = [
            sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", str(BACKEND_ROOT),
            "--host", "127.0.0.1", "--port", str(args.port),
        ]
        return run_child(command, env=env, cwd=BACKEND_ROOT, duration=args.duration)


if __name__ == "__main__":
    raise SystemExit(main())
