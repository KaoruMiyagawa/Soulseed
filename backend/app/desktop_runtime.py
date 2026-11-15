"""Packaged desktop bootstrap. Its settings come only from the native host."""

from __future__ import annotations

import ctypes
import json
import sqlite3
from contextlib import closing
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, Field, SecretStr

from .config import Settings
from .db import MIGRATIONS, Database
from .model_budget import BudgetRejected, ModelBudgetService
from .ownership import character_scope

_DATABASE_ID = 0x534F554C
runtime: DesktopRuntime | None = None


class DesktopSettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, **_):
        return (init_settings,)


class DesktopDatabase(Database):
    def initialize(self) -> None:
        # LifeInstance holds the existing exclusive lock across initialization.
        target = Path(self.path)
        if target.is_symlink() or self._protected_database_target() is not None:
            raise RuntimeError("桌面数据目录中存在受保护的数据库链接")
        fresh = not target.exists() or target.stat().st_size == 0
        with closing(sqlite3.connect(target)) as db, db:
            if fresh:
                db.execute(f"PRAGMA application_id = {_DATABASE_ID}")
            elif db.execute("PRAGMA application_id").fetchone()[0] != _DATABASE_ID:
                raise RuntimeError("此数据库不是由桌面版创建的")
            has_schema = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            current = db.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0] if has_schema else 0
            if not 0 <= current <= max(MIGRATIONS):
                raise RuntimeError("此桌面数据库版本不受支持")
            if 0 < current < max(MIGRATIONS):
                backup = target.with_suffix(f".schema-{current}.backup")
                if backup.is_symlink():
                    raise RuntimeError("桌面数据库备份路径包含链接")
                with closing(sqlite3.connect(backup)) as output:
                    db.backup(output)
        if current < max(MIGRATIONS):
            self._apply_migrations()
        with self.atomic() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS desktop_model_config (
                    id INTEGER PRIMARY KEY CHECK(id=1), ciphertext BLOB NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS desktop_runtime_limits (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    daily_limit INTEGER NOT NULL CHECK(daily_limit BETWEEN 1 AND 20),
                    autonomous_paused INTEGER NOT NULL CHECK(autonomous_paused IN (0,1))
                )
            """)
            db.execute("INSERT OR IGNORE INTO desktop_runtime_limits(id,daily_limit,autonomous_paused) VALUES(1,20,1)")
            if "contact_enabled" not in {row["name"] for row in db.execute("PRAGMA table_info(desktop_runtime_limits)")}:
                db.execute("ALTER TABLE desktop_runtime_limits ADD COLUMN contact_enabled INTEGER NOT NULL DEFAULT 0 CHECK(contact_enabled IN (0,1))")
            db.execute("""
                CREATE TABLE IF NOT EXISTS desktop_contact_notifications (
                    message_id TEXT PRIMARY KEY REFERENCES proactive_messages(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS desktop_generated_reviews (
                    draft_id TEXT PRIMARY KEY, character_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL, content_sha256 TEXT NOT NULL,
                    evidence_json TEXT NOT NULL, status TEXT NOT NULL,
                    reason TEXT NOT NULL, expires_at TEXT NOT NULL
                )
            """)


class DesktopBudget(ModelBudgetService):
    def __init__(self, database: DesktopDatabase):
        if type(database) is not DesktopDatabase or database.environment != "production":
            raise ValueError("desktop_budget_requires_desktop_database")
        super().__init__(database, global_limit=20, source_limits={"chat": 20})

    def _window(self, db, day):
        # This database is created by DesktopDatabase and has always used the
        # SQLite ledger. Never open a window that already exists in a closed state.
        db.execute(
            "INSERT OR IGNORE INTO model_budget_windows(day,policy_json,history_ready) VALUES(?,?,1)",
            (day, self.policy),
        )
        return super()._window(db, day)

    def preferences(self) -> dict:
        with self.database.connection() as db:
            row = db.execute("SELECT daily_limit,autonomous_paused,contact_enabled FROM desktop_runtime_limits WHERE id=1").fetchone()
        return {"daily_limit": row["daily_limit"], "autonomous_paused": bool(row["autonomous_paused"]),
                "contact_enabled": bool(row["contact_enabled"])}

    def require_autonomous_allowed(self, source):
        preferences = self.preferences()
        if preferences["autonomous_paused"] or source == "autonomous" and not preferences["contact_enabled"]:
            raise BudgetRejected("autonomous_paused")

    def reserve(self, request_id, character_id, source, *, autonomous_reservation_id=None, expected_day=None):
        # Serialize the adjustable cap with the durable reservation. The base
        # policy remains a hard 20-request ceiling across upgrades and days.
        day = self._now().date().isoformat()
        with self.database.atomic() as db:
            if source in {"autonomous", "autonomous_exploration"}:
                self.require_autonomous_allowed(source)
            existing = db.execute("SELECT 1 FROM model_budget_requests WHERE id=?", (request_id,)).fetchone()
            if not existing:
                preferences = db.execute("SELECT daily_limit FROM desktop_runtime_limits WHERE id=1").fetchone()
                used = db.execute("SELECT COUNT(*) FROM model_budget_requests WHERE day=? AND status<>'released'", (day,)).fetchone()[0]
                if used >= preferences["daily_limit"]:
                    raise BudgetRejected("global_budget_exhausted")
                if source in {"exploration", "autonomous_exploration"}:
                    exploration_used = db.execute("SELECT COUNT(*) FROM model_budget_requests WHERE day=? AND source IN ('exploration','autonomous_exploration') AND status<>'released'", (day,)).fetchone()[0]
                    if exploration_used >= 8:
                        raise BudgetRejected("source_budget_exhausted")
            return super().reserve(request_id, character_id, source,
                                   autonomous_reservation_id=autonomous_reservation_id,
                                   expected_day=expected_day)

    def mark_started(self, request_id, character_id, *, claim_token=None):
        from .autonomous_intent_state import AutonomousIntentStateRepository

        with self.database.atomic() as db:
            row = db.execute("""SELECT b.source,r.candidate_id
                FROM model_budget_requests b LEFT JOIN autonomous_contact_reservations r
                    ON r.id=b.autonomous_reservation_id
                WHERE b.id=? AND b.character_id=?""", (request_id, character_id)).fetchone()
            if row and row["source"] in {"autonomous", "autonomous_exploration"}:
                self.require_autonomous_allowed(row["source"])
                if row["source"] == "autonomous":
                    if not claim_token or not row["candidate_id"]:
                        raise BudgetRejected("autonomous_reservation_mismatch")
                    state = AutonomousIntentStateRepository(self.database, enabled=True, allow_first_discovery=True)
                    started = state.mark_request_started(row["candidate_id"], character_id, claim_token)
                    if not started.allowed:
                        raise BudgetRejected(started.reason_code)
            # The desktop-specific recheck above already started the D2 reservation.
            return self._transition(request_id, character_id, 'started')


class ModelInput(BaseModel):
    base_url: str = Field(max_length=2048)
    model: str = Field(max_length=256)
    api_key: SecretStr = Field(max_length=4096)


class RuntimeLimitsInput(BaseModel):
    daily_limit: int = Field(ge=1, le=20)
    autonomous_paused: bool
    contact_enabled: bool | None = None


class _Blob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.c_void_p)]


def _dpapi(data: bytes, *, encrypt: bool) -> bytes:
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.addressof(buffer))
    result = _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    function.argtypes = (
        ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob),
    )
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN, with the default current-user scope.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.length)
    finally:
        ctypes.memset(result.data, 0, result.length)
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = (ctypes.c_void_p,)
        free.restype = ctypes.c_void_p
        free(result.data)


class DesktopRuntime:
    def __init__(self, data_dir: Path, token: str):
        self.token = token
        chat_output_tokens = 1024
        self.settings = DesktopSettings(
            environment="production", database_path=str(data_dir / "soulseed-desktop.db"),
            model_enabled=False, model_api_key=None, model_trust_env=False,
            model_max_output_tokens=chat_output_tokens, chat_async_max_output_tokens=chat_output_tokens,
            exploration_enabled=True,
            contact_enabled=False, contact_background_enabled=False,
        )
        self.database = DesktopDatabase(self.settings.database_path, environment="production")
        self.budget = DesktopBudget(self.database)

    def _load(self) -> dict | None:
        with self.database.connection() as db:
            row = db.execute("SELECT ciphertext FROM desktop_model_config WHERE id=1").fetchone()
        if not row:
            return None
        try:
            return json.loads(_dpapi(row["ciphertext"], encrypt=False))
        except (ValueError, OSError) as exc:
            raise HTTPException(503, "当前 Windows 用户无法读取模型配置，请重新保存。") from exc

    def status(self) -> dict:
        model = self._load()
        return {"configured": bool(model), "base_url": model["base_url"] if model else "",
                "model": model["model"] if model else ""}

    def runtime_status(self) -> dict:
        with character_scope("big_fish"):
            usage = self.budget.usage_snapshot("big_fish")
        preferences = self.budget.preferences()
        return {**preferences, "day": usage["day"], "used": usage["global_used"],
                "remaining": max(0, preferences["daily_limit"] - usage["global_used"]),
                "exploration_used": (usage["source_used"].get("exploration", 0)
                                     + usage["source_used"].get("autonomous_exploration", 0)),
                "contact_used": usage["source_used"].get("autonomous", 0),
                "chat_used": usage["source_used"].get("chat", 0)}

    def save_runtime_limits(self, request: RuntimeLimitsInput) -> dict:
        with self.database.atomic() as db:
            db.execute("""UPDATE desktop_runtime_limits SET daily_limit=?,autonomous_paused=?,
                          contact_enabled=COALESCE(?,contact_enabled) WHERE id=1""",
                       (request.daily_limit, int(request.autonomous_paused), request.contact_enabled))
        return self.runtime_status()

    def model_settings(self) -> Settings:
        model = self._load()
        if not model:
            raise HTTPException(409, "请先在「模型设置」保存你自己的模型配置。")
        return self.settings.model_copy(update={
            "model_enabled": True, "model_base_url": model["base_url"],
            "model_name": model["model"], "model_api_key": model["api_key"],
        })

    def save(self, request: ModelInput) -> dict:
        url, model, key = request.base_url.strip().rstrip("/"), request.model.strip(), request.api_key.get_secret_value().strip()
        try:
            parsed = urlsplit(url)
            localhost = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
            valid_url = (parsed.hostname and (parsed.scheme == "https" or parsed.scheme == "http" and localhost)
                         and parsed.port != 0 and parsed.username is None and parsed.password is None
                         and not any(char.isspace() or char in "\\?#" for char in url))
        except ValueError:
            valid_url = False
        if not valid_url or not model or not key or any(not 33 <= ord(char) <= 126 for char in key):
            raise HTTPException(422, "请填写有效的 HTTPS API 基础地址、模型 ID 和 API Key。本机服务可以使用 HTTP。")
        payload = {"base_url": url, "model": model, "api_key": key}
        try:
            encrypted = _dpapi(json.dumps(payload).encode(), encrypt=True)
            with self.database.atomic() as db:
                db.execute(
                    "INSERT INTO desktop_model_config(id,ciphertext) VALUES(1,?) "
                    "ON CONFLICT(id) DO UPDATE SET ciphertext=excluded.ciphertext",
                    (encrypted,),
                )
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(503, "Windows 无法安全保存模型配置。") from exc
        return {"configured": True, "base_url": url, "model": model}

    def clear(self) -> dict:
        with self.database.atomic() as db:
            db.execute("DELETE FROM desktop_model_config WHERE id=1")
        return {"configured": False, "base_url": "", "model": ""}
