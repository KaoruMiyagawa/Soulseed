"""Local single-instance ownership for one SQLite life database.

The OS byte lock is the liveness fence. The SQLite row is an auditable lease;
its expiry alone never authorizes takeover while the process lock is held.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import socket
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .db import Database
from .life import LifeRepository, _iso

logger = logging.getLogger(__name__)


class InstanceConflict(RuntimeError):
    pass


class LifeInstance:
    _guard = threading.Lock()
    _held_paths: set[str] = set()

    def __init__(self, database: Database, repository: LifeRepository,
                 *, lease_duration: timedelta = timedelta(minutes=15)):
        if repository.database is not database:
            raise ValueError("活动仓库必须使用同一个数据库对象")
        if lease_duration <= timedelta(0):
            raise ValueError("租约时长必须为正数")
        self.database = database
        self.repository = repository
        self.lease_duration = lease_duration
        self.path = str(Path(database.path).resolve())
        self._file = None
        self.token: str | None = None

    @staticmethod
    def _lock(file) -> None:
        file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(file) -> None:
        file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def _release_file(self) -> None:
        file, self._file = self._file, None
        if file is not None:
            try:
                self._unlock(file)
            finally:
                file.close()
                with self._guard:
                    self._held_paths.discard(self.path)

    def acquire(self, *, now: datetime | None = None) -> str:
        if self._file is not None:
            raise RuntimeError("实例已经持有数据库所有权")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._guard:
            if self.path in self._held_paths:
                raise InstanceConflict("数据库已由本进程的另一个服务实例运行")
            file = open(self.path + ".instance.lock", "a+b")
            try:
                if file.tell() == 0:
                    file.write(b"0")
                    file.flush()
                self._lock(file)
            except (OSError, BlockingIOError) as exc:
                file.close()
                raise InstanceConflict("数据库已由另一个服务实例运行") from exc
            self._held_paths.add(self.path)
            self._file = file
        try:
            self.database.initialize()
            current = now or datetime.now(UTC)
            token = str(uuid4())
            at = _iso(current)
            with self.database.atomic() as db:
                # The OS lock proves the previous holder has exited, even if
                # its persisted lease has not expired yet.
                db.execute("INSERT INTO life_service_instances(id, token, process_id, host_name, acquired_at, heartbeat_at, expires_at) VALUES (1, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET token=excluded.token, process_id=excluded.process_id, host_name=excluded.host_name, acquired_at=excluded.acquired_at, heartbeat_at=excluded.heartbeat_at, expires_at=excluded.expires_at",
                           (token, os.getpid(), socket.gethostname(), at, at, _iso(current + self.lease_duration)))
                # An abnormal exit may leave a still-unexpired per-character
                # advance lease. The exclusive process lock proves it is stale.
                db.execute("DELETE FROM life_advance_leases")
                self.repository.interrupt_active(owner_token=token, reason="owner_lost", now=current, orphaned=True)
            self.token = token
            self.repository.instance_token = token
            self.repository.instance_owner = self
            return token
        except Exception:
            self._release_file()
            raise

    def assert_owned(self) -> None:
        if self._file is None or self.token is None:
            raise InstanceConflict("当前服务实例没有数据库所有权")
        with self.database.connection() as db:
            row = db.execute("SELECT token FROM life_service_instances WHERE id = 1").fetchone()
            if row is None or row["token"] != self.token:
                raise InstanceConflict("数据库实例租约已转移")

    def heartbeat(self, *, now: datetime | None = None) -> None:
        self.assert_owned()
        current = now or datetime.now(UTC)
        with self.database.atomic() as db:
            changed = db.execute("UPDATE life_service_instances SET heartbeat_at = ?, expires_at = ? WHERE id = 1 AND token = ?",
                                 (_iso(current), _iso(current + self.lease_duration), self.token)).rowcount
            if changed != 1:
                raise InstanceConflict("数据库实例租约已转移")

    async def run_heartbeat(self, stop: asyncio.Event, *, interval: timedelta = timedelta(minutes=5)) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval.total_seconds())
            except TimeoutError:
                try:
                    self.heartbeat()
                except sqlite3.OperationalError:
                    # A temporary SQLite writer conflict must not silently
                    # kill the heartbeat loop. The OS lock still fences peers.
                    logger.exception("实例心跳写入失败，将在下个低频周期重试")
                except InstanceConflict:
                    stop.set()
                    raise

    def release(self, *, now: datetime | None = None) -> None:
        if self._file is None:
            return
        try:
            self.assert_owned()
            with self.database.atomic() as db:
                self.repository.interrupt_active(owner_token=self.token, reason="service_stopped", now=now)
                changed = db.execute("DELETE FROM life_service_instances WHERE id = 1 AND token = ?", (self.token,)).rowcount
                if changed != 1:
                    raise InstanceConflict("数据库实例租约已转移")
        finally:
            self.repository.instance_token = None
            self.repository.instance_owner = None
            self.token = None
            self._release_file()
