"""Run one isolated local Soulseed frontend/backend pair from a user terminal.

The random pairing secret exists only in this process and the backend child
environment. Run interactively: stdout must be a terminal and is never logged.
Ctrl+C stops only the two children created here and removes their Temp data.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
BACKEND_PORT = 18081
FRONTEND_PORT = 5175
API_URL = f"http://127.0.0.1:{BACKEND_PORT}"
FRONTEND_URL = f"http://127.0.0.1:{FRONTEND_PORT}/"

sys.path.insert(0, str(BACKEND_ROOT))
from app.config import validate_test_database_path  # noqa: E402
from run_isolated import isolated_environment  # noqa: E402


def require_free_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"本地端口 {port} 已占用；未启动或停止任何现有服务") from exc


def response(url: str, *, data: bytes | None = None, origin: str | None = None) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if origin:
        headers["Origin"] = origin
    request = Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    with urlopen(request, timeout=2) as result:
        return result.status, result.read()


def wait_ready(children: list[subprocess.Popen]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if any(child.poll() is not None for child in children):
            raise RuntimeError("独立测试服务提前退出；现有服务未受影响")
        try:
            api_ok = response(API_URL + "/api/health")[0] == 200
            frontend_ok = response(FRONTEND_URL)[0] == 200
            if api_ok and frontend_ok:
                return
        except (OSError, URLError, TimeoutError):
            pass
        time.sleep(0.25)
    raise RuntimeError("独立测试服务启动超时；现有服务未受影响")


def stop_children(children: list[subprocess.Popen]) -> None:
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
    for child in reversed(children):
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def main() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("请在本机交互式 PowerShell 运行；口令不会写入重定向输出或日志")
    node = shutil.which("node")
    vite = FRONTEND_ROOT / "node_modules" / "vite" / "bin" / "vite.js"
    if not node or not vite.is_file():
        raise RuntimeError("前端依赖未就绪；不会自动安装依赖")
    require_free_port(BACKEND_PORT)
    require_free_port(FRONTEND_PORT)

    with tempfile.TemporaryDirectory(prefix="soulseed-pairing-") as temporary:
        root = Path(temporary).resolve(strict=True)
        validate_test_database_path(root / "soulseed-test.db", root)
        backend_env = isolated_environment(root, BACKEND_PORT)
        backend_env["SOULSEED_TEST_FRONTEND_ORIGIN"] = FRONTEND_URL.rstrip("/")
        backend_env["SOULSEED_CONTACT_MODEL_GENERATION_ENABLED"] = "false"
        secret = backend_env["SOULSEED_LOCAL_PAIRING_SECRET"]
        if len(secret) < 32:
            raise RuntimeError("测试配对口令长度不足")
        frontend_env = {key: value for key, value in os.environ.items()
                        if not key.upper().startswith("SOULSEED_")
                        and key.upper() not in {"OPENAI_API_KEY", "SILICONFLOW_API_KEY", "AZURE_OPENAI_API_KEY"}}
        frontend_env["VITE_API_URL"] = API_URL + "/api"
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        children: list[subprocess.Popen] = []
        try:
            frontend = subprocess.Popen(
                [node, str(vite), "--host", "127.0.0.1", "--port", str(FRONTEND_PORT), "--strictPort"],
                cwd=FRONTEND_ROOT, env=frontend_env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
            )
            children.append(frontend)
            backend = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", str(BACKEND_ROOT),
                 "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
                cwd=BACKEND_ROOT, env=backend_env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
            )
            children.append(backend)
            wait_ready(children)
            _, openapi = response(API_URL + "/openapi.json")
            if "post" not in json.loads(openapi)["paths"].get("/api/auth/session", {}):
                raise RuntimeError("测试后端缺少配对接口")
            _, served = response(FRONTEND_URL + "src/main.tsx")
            if (API_URL + "/api").encode() not in served:
                raise RuntimeError("测试前端 API 地址不匹配")
            request_body = json.dumps({"secret": secret, "character_id": "big_fish"}).encode("utf-8")
            pair_status, _ = response(API_URL + "/api/auth/session", data=request_body,
                                      origin=FRONTEND_URL.rstrip("/"))
            if pair_status != 200:
                raise RuntimeError("测试口令配对检查失败")
            print(f"独立测试前端：{FRONTEND_URL}")
            print("测试数据库：系统 Temp 下独立目录（testing 模式）")
            print("本地配对口令（仅显示在此终端，请复制到网页）：")
            print(secret, flush=True)
            print("保持此终端打开以运行测试实例；完成后按 Ctrl+C 停止并清理本次临时数据。", flush=True)
            while all(child.poll() is None for child in children):
                time.sleep(1)
            raise RuntimeError("独立测试服务意外退出")
        except KeyboardInterrupt:
            print("\n正在停止本次测试实例。", flush=True)
            return 0
        finally:
            stop_children(children)


if __name__ == "__main__":
    raise SystemExit(main())
