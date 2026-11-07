"""Run the persistent, local Soulseed development pair from one terminal."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
DATABASE = BACKEND / "data" / "soulseed-development.db"
FRONTEND_PORT = 5174
BACKEND_PORT = 8001
MODEL_KEYS = {"SOULSEED_MODEL_API_KEY", "SOULSEED_MODEL_BASE_URL", "SOULSEED_MODEL_NAME"}


def _ensure_free(port: int) -> None:
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"127.0.0.1:{port} 已被占用；未启动或停止任何服务。") from exc


def _child_environment(*, model_enabled: bool = True,
                       contact_model_generation_enabled: bool = False) -> dict[str, str]:
    # The parent process, including an old SOULSEED_DATABASE_PATH, is never
    # allowed to override this launcher's fixed database target.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("SOULSEED_") and key.upper() != "VITE_API_URL"}
    model = dotenv_values(ROOT / ".env")
    env.update({key: str(model[key]) for key in MODEL_KEYS if model.get(key)})
    env.update({
        "SOULSEED_ENVIRONMENT": "development",
        "SOULSEED_DATABASE_PATH": str(DATABASE.resolve(strict=False)),
        "SOULSEED_HOST": "127.0.0.1",
        "SOULSEED_PORT": str(BACKEND_PORT),
        "SOULSEED_DEVELOPMENT_FRONTEND_ORIGIN": f"http://127.0.0.1:{FRONTEND_PORT}",
        "SOULSEED_MODEL_ENABLED": "true" if model_enabled else "false",
        "SOULSEED_MODEL_MAX_OUTPUT_TOKENS": "1024",
        "SOULSEED_MODEL_DAILY_REQUEST_LIMIT": "20",
        "SOULSEED_MODEL_CONVERSATION_TURN_LIMIT": "100",
        "SOULSEED_MODEL_REQUEST_LOG_PATH": str(Path(os.environ["LOCALAPPDATA"]) / "Soulseed" / "development" / "model-requests.jsonl"),
        "SOULSEED_EXPLORATION_MODEL_REQUEST_LOG_PATH": str(Path(os.environ["LOCALAPPDATA"]) / "Soulseed" / "development" / "exploration-model-requests.jsonl"),
        "SOULSEED_CHAT_TIMING_LOG_PATH": str(Path(os.environ["LOCALAPPDATA"]) / "Soulseed" / "development" / "chat-timings.jsonl"),
        "SOULSEED_CONTACT_MODEL_GENERATION_ENABLED": "true" if contact_model_generation_enabled else "false",
        "VITE_API_URL": f"http://127.0.0.1:{BACKEND_PORT}/api",
        "VITE_SOULSEED_DEVELOPMENT_MODE": "true",
    })
    return env


def _frontend_environment(backend_env: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in backend_env.items()
            if not key.upper().startswith("SOULSEED_")}


def _preflight(env: dict[str, str], python: Path) -> str:
    if not python.is_file():
        raise RuntimeError("未找到项目 .venv Python；请先使用现有项目环境。")
    if not (FRONTEND / "node_modules" / "vite" / "bin" / "vite.js").is_file():
        raise RuntimeError("未找到现有 Vite 依赖；未自动安装新依赖。")
    if not env.get("SOULSEED_MODEL_API_KEY"):
        raise RuntimeError("项目 .env 缺少 SOULSEED_MODEL_API_KEY；未启动开发服务。")
    code = (
        "import os; from app.config import get_settings, DEVELOPMENT_DATABASE_PATH; "
        "from pathlib import Path; s=get_settings(); "
        "assert s.environment=='development'; "
        "assert Path(s.database_path).resolve()==DEVELOPMENT_DATABASE_PATH.resolve(); "
        "assert s.model_api_key and s.model_base_url and s.model_name; "
        "assert s.local_pairing_secret is None; "
        "assert s.contact_model_generation_enabled == (os.environ.get('SOULSEED_CONTACT_MODEL_GENERATION_ENABLED') == 'true'); "
        "assert s.development_frontend_origin=='http://127.0.0.1:5174'; "
        "from app.build_info import BUILD_ID; print(BUILD_ID)"
    )
    check = subprocess.run([str(python), "-c", code], cwd=BACKEND, env=env,
                           check=True, capture_output=True, text=True)
    _ensure_free(FRONTEND_PORT)
    _ensure_free(BACKEND_PORT)
    return check.stdout.strip().splitlines()[-1]


def _stop(children: list[subprocess.Popen]) -> None:
    for child in children:
        if child.poll() is None:
            child.terminate()
    for child in children:
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Only a child created by this process may be forced to exit.
            child.kill()
            child.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-off", action="store_true", help="disable all paid model requests for this run")
    parser.add_argument("--contact-model-generation", action="store_true",
                        help="explicitly allow model-generated proactive messages; may incur model usage")
    args = parser.parse_args()
    if args.model_off and args.contact_model_generation:
        raise RuntimeError("--contact-model-generation 需要启用模型；请移除 --model-off 并确认愿意承担模型调用。")
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    env = _child_environment(model_enabled=not args.model_off,
                             contact_model_generation_enabled=args.contact_model_generation)
    build_id = _preflight(env, python)
    frontend_env = _frontend_environment(env)
    node = shutil.which("node")
    if not node:
        raise RuntimeError("未找到 Node.js；未启动开发服务。")
    print("Soulseed environment=development database=<backend/data>/soulseed-development.db", flush=True)
    print(f"前端 http://127.0.0.1:{FRONTEND_PORT}  后端 http://127.0.0.1:{BACKEND_PORT}", flush=True)
    print(f"开发代码标识 build_id={build_id}；启动后可与 /api/health 的 build_id 对照。", flush=True)
    model_state = "已关闭" if args.model_off else "已启用"
    print(f"模型{model_state}；每次最多 1024 输出 Token，每 UTC 日最多 20 次请求；调用记录位于本机 LOCALAPPDATA/Soulseed/development。", flush=True)
    print(f"主动消息模型表达{'已显式开启' if args.contact_model_generation else '默认关闭'}；只有已有联系规则批准的消息才会调用。", flush=True)
    print("后台自主探索默认关闭；可在 5174 开启。仅后端运行时按 LifeScheduler 检查，默认最多每 24 小时一次；每任务最多 4 次决策请求、每 UTC 日最多 8 次探索请求、每次最多 512 输出 Token，调用记录单独保存。", flush=True)
    children: list[subprocess.Popen] = []
    try:
        children.append(subprocess.Popen(
            [str(python), "-m", "uvicorn", "app.main:app", "--app-dir", str(BACKEND),
             "--host", "127.0.0.1", "--port", str(BACKEND_PORT)], cwd=BACKEND, env=env))
        children.append(subprocess.Popen(
            [node, str(FRONTEND / "node_modules" / "vite" / "bin" / "vite.js"),
             "--host", "127.0.0.1", "--port", str(FRONTEND_PORT), "--strictPort"],
            cwd=FRONTEND, env=frontend_env))
        print("按 Ctrl+C 仅停止本启动器创建的前后端进程。", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
        return next((child.returncode for child in children if child.returncode), 1)
    except KeyboardInterrupt:
        return 0
    finally:
        _stop(children)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"开发启动失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
