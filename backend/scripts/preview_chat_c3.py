"""Launch a disposable Mock chat preview or run its Playwright checks."""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen

from run_isolated import BACKEND_ROOT, PROJECT_ROOT, isolated_environment


def available_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def wait_for(url: str, process: subprocess.Popen) -> bytes:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and process.poll() is None:
        try:
            with urlopen(url, timeout=1) as response:
                return response.read()
        except (URLError, TimeoutError):
            time.sleep(0.1)
    raise RuntimeError(f"隔离预览未能启动：{url}")


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="run Playwright and exit")
    parser.add_argument("--artifacts", type=Path, default=Path(tempfile.gettempdir()) / "soulseed-chat-c3-evidence")
    args = parser.parse_args()
    if not args.test:
        parser.error("此脚本仅供隔离自动化测试，不提供额外的用户预览入口。")
    frontend = PROJECT_ROOT / "frontend"
    vite = frontend / "node_modules" / "vite" / "bin" / "vite.js"
    playwright = frontend / "node_modules" / "@playwright" / "test" / "cli.js"
    if not vite.is_file() or args.test and not playwright.is_file():
        parser.error("请先在 frontend 目录执行 npm ci")
    api_port, web_port = available_port(), available_port()
    while web_port == api_port:
        web_port = available_port()
    api = f"http://127.0.0.1:{api_port}/api"
    web = f"http://127.0.0.1:{web_port}"
    with tempfile.TemporaryDirectory(prefix="soulseed-testing-") as directory, ExitStack() as cleanup:
        env = isolated_environment(Path(directory), api_port)
        env.update({
            "SOULSEED_TEST_FRONTEND_ORIGIN": web,
            "SOULSEED_CHAT_ASYNC_MOCK_ENABLED": "true",
            "SOULSEED_CHAT_ASYNC_MOCK_DELAY_SECONDS": "1.5",
            "SOULSEED_MODEL_ENABLED": "false",
            "SOULSEED_CONTACT_ENABLED": "false",
        })
        backend = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(api_port),
             "--no-access-log", "--log-level", "warning"], cwd=BACKEND_ROOT, env=env,
        )
        cleanup.callback(stop, backend)
        health = json.loads(wait_for(f"{api}/health", backend))
        if health["environment"] != "testing" or health["chat_reply_mode"] != "mock":
            raise RuntimeError("预览要求隔离测试环境和 Mock Worker")
        web_env = {**os.environ, "VITE_API_URL": api, "VITE_SOULSEED_DEVELOPMENT_MODE": "false"}
        server = subprocess.Popen(
            ["node", str(vite), "--host", "127.0.0.1", "--port", str(web_port), "--strictPort"],
            cwd=frontend, env=web_env,
        )
        cleanup.callback(stop, server)
        wait_for(web, server)
        artifacts = args.artifacts.resolve()
        artifacts.mkdir(parents=True, exist_ok=True)
        web_env.update({
            "CHAT_C3_API_URL": api, "CHAT_C3_WEB_URL": web,
            "CHAT_C3_PAIRING_SECRET": env["SOULSEED_LOCAL_PAIRING_SECRET"],
            "CHAT_C3_ARTIFACTS": str(artifacts),
        })
        return subprocess.call(
            ["node", str(playwright), "test", "--config", "playwright.chat.config.ts"],
            cwd=frontend, env=web_env,
        )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
