"""Run browser acceptance for shared autonomous social with an isolated DB and local mock."""

from __future__ import annotations

from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen

from run_isolated import BACKEND_ROOT, PROJECT_ROOT, isolated_environment

mock_server: ThreadingHTTPServer | None = None

def available_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def wait_for(url: str, process: subprocess.Popen) -> bytes:
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline and process.poll() is None:
        try:
            with urlopen(url, timeout=1) as response:
                return response.read()
        except (URLError, TimeoutError, OSError):
            time.sleep(0.1)
    raise RuntimeError(f"隔离服务未能启动：{url}")


def main() -> int:
    global mock_server
    frontend = PROJECT_ROOT / "frontend"
    vite = frontend / "node_modules" / "vite" / "bin" / "vite.js"
    playwright = frontend / "node_modules" / "@playwright" / "test" / "cli.js"
    if not vite.is_file() or not playwright.is_file():
        raise RuntimeError("缺少前端依赖；请先在 frontend 目录运行 npm ci")

    api_port, web_port, mock_port = available_port(), available_port(), available_port()
    while web_port in {api_port, mock_port} or mock_port == api_port:
        web_port, mock_port = available_port(), available_port()
    api, web = f"http://127.0.0.1:{api_port}/api", f"http://127.0.0.1:{web_port}"
    calls: list[dict] = []

    class MockHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            content = "\n".join(str(item.get("content", "")) for item in body.get("messages", []))
            try:
                signals = json.loads(body["messages"][-1]["content"])
            except (ValueError, KeyError, IndexError, TypeError):
                signals = None
            if isinstance(signals, dict) and signals.get("candidate_type") == "self_social":
                choice = signals["choices"][0]
                answer = json.dumps({
                    "decision": "contact", "choice": choice["id"], "style": "curious",
                    "message": "刚才看着今天的天空，我突然想给你送一颗快乐泡泡。你今天过得怎么样？",
                    "evidence_ids": choice["evidence_ids"], "claims": [],
                }, ensure_ascii=False)
                kind = "autonomous_social"
            else:
                answer, kind = "我今天过得很开心，谢谢你来找我聊！", "chat_reply"
            calls.append({"kind": kind, "max_tokens": body.get("max_tokens"),
                          "candidate_type": signals.get("candidate_type") if isinstance(signals, dict) else None,
                          "has_persona": "大肥鱼" in content or "淘气" in content})
            payload = json.dumps({"choices": [{"message": {"content": answer}, "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    mock_server = ThreadingHTTPServer(("127.0.0.1", mock_port), MockHandler)
    threading.Thread(target=mock_server.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory(prefix="soulseed-autonomous-browser-") as directory, ExitStack() as cleanup:
        test_root = Path(directory).resolve(strict=True)
        env = isolated_environment(test_root, api_port)
        pairing_secret = secrets.token_urlsafe(32)
        env.update({
            "SOULSEED_TEST_FRONTEND_ORIGIN": web,
            "SOULSEED_DATABASE_PATH": str(test_root / "soulseed-test.db"),
            "SOULSEED_LOCAL_PAIRING_SECRET": pairing_secret,
            "SOULSEED_MODEL_BASE_URL": f"http://127.0.0.1:{mock_port}/v1",
            "SOULSEED_MODEL_API_KEY": "isolated-browser-mock-only",
            "SOULSEED_MODEL_NAME": "isolated-browser-mock",
            "SOULSEED_MODEL_TRUST_ENV": "false",
            "SOULSEED_MODEL_ENABLED": "true",
            "SOULSEED_CONTACT_ENABLED": "true",
            "SOULSEED_CONTACT_BACKGROUND_ENABLED": "true",
            "SOULSEED_CONTACT_MODEL_GENERATION_ENABLED": "true",
            "SOULSEED_CONTACT_CHECK_INTERVAL_MINUTES": "30",
            "SOULSEED_LIFE_BACKGROUND_ENABLED": "false",
        })

        # Initialize only the fenced test DB, then give the scheduler a real character state and chat.
        seed_code = r'''
from app.config import get_settings
from app.db import Database
from app.life import LifeRepository
from app.repositories import ConversationRepository
from app.contact import ContactService
s = get_settings()
d = Database(s.database_path, environment="testing", test_data_root=s.test_data_root)
d.initialize()
l = LifeRepository(d, default_timezone=s.life_timezone)
l.ensure_state("big_fish")
c = ConversationRepository(d).create("自主社交浏览器验收")
with d.atomic() as q:
    q.execute("UPDATE life_states SET mood='好奇' WHERE character_id='big_fish'")
ContactService(l).set_preferences("big_fish", allowed=True, quiet_start_minute=0, quiet_end_minute=0)
print(c["id"])
'''
        seed = subprocess.run([sys.executable, "-c", seed_code], cwd=BACKEND_ROOT, env=env,
                              check=True, capture_output=True, text=True)
        conversation_id = seed.stdout.strip().splitlines()[-1]

        backend = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(api_port),
             "--no-access-log", "--log-level", "warning"], cwd=BACKEND_ROOT, env=env,
        )
        cleanup.callback(stop, backend)
        health = json.loads(wait_for(f"{api}/health", backend))
        if health.get("environment") != "testing":
            raise RuntimeError("浏览器验收后端没有处于 testing 隔离环境")

        web_env = {**os.environ, "VITE_API_URL": api, "VITE_SOULSEED_DEVELOPMENT_MODE": "false"}
        server = subprocess.Popen(
            ["node", str(vite), "--host", "127.0.0.1", "--port", str(web_port), "--strictPort"],
            cwd=frontend, env=web_env,
        )
        cleanup.callback(stop, server)
        wait_for(web, server)

        artifacts = PROJECT_ROOT / "desktop-build" / "qa" / "browser-autonomous-social"
        artifacts.mkdir(parents=True, exist_ok=True)
        test_env = {
            **web_env, "CHAT_C3_API_URL": api, "CHAT_C3_WEB_URL": web,
            "CHAT_C3_PAIRING_SECRET": pairing_secret,
            "CHAT_C3_ARTIFACTS": str(artifacts),
            "SOULSEED_QA_CONVERSATION_ID": conversation_id,
        }
        result = subprocess.run(
            ["node", str(playwright), "test", "e2e/autonomous-social.spec.ts",
             "--config", "playwright.chat.config.ts"], cwd=frontend, env=test_env,
        )

        kinds = [call["kind"] for call in calls]
        if result.returncode == 0 and ("autonomous_social" not in kinds or "chat_reply" not in kinds):
            raise RuntimeError(f"浏览器测试未经过自主决策和聊天回复模型闭环：{kinds}")
        if result.returncode == 0:
            print("浏览器验收通过：角色状态→共享自主联系模型→原会话消息→浏览器用户回复。")
            print(f"本机模拟模型调用：{kinds}；测试数据库仅在临时目录内。")
        return result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        try:
            if mock_server:
                mock_server.shutdown()
                mock_server.server_close()
        except NameError:
            pass
