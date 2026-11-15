"""PyInstaller entry point for the Windows desktop application."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import sys
from pathlib import Path
from threading import Thread

import uvicorn

from app import desktop_runtime


async def serve() -> None:
    # The pipe is inherited from the desktop host. EOF also requests shutdown.
    bootstrap = json.loads(sys.stdin.buffer.readline(32768))
    data_dir = Path(bootstrap["data_dir"])
    token = bootstrap["token"]
    if not data_dir.is_absolute() or not data_dir.is_dir() or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("invalid desktop bootstrap")
    desktop_runtime.runtime = desktop_runtime.DesktopRuntime(data_dir, token)
    logging.disable(logging.CRITICAL)
    from app.main import app

    server = uvicorn.Server(uvicorn.Config(
        app, loop="asyncio", http="h11", ws="none", lifespan="on",
        proxy_headers=False, access_log=False, log_config=None,
        timeout_graceful_shutdown=2, server_header=False,
    ))
    loop = asyncio.get_running_loop()

    def wait_for_host() -> None:
        try:
            sys.stdin.buffer.read(1)
        finally:
            loop.call_soon_threadsafe(setattr, server, "should_exit", True)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        print(json.dumps({"port": listener.getsockname()[1]}), flush=True)
        Thread(target=wait_for_host, daemon=True).start()
        await server.serve(sockets=[listener])
        if not server.started:
            raise RuntimeError("desktop backend did not start")


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except Exception:
        # The host presents a generic failure. Never print request or config data.
        sys.exit(1)
