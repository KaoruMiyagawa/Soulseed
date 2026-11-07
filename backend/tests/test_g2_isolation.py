from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import DEVELOPMENT_DATABASE_PATH, FORMAL_DATABASE_PATHS, Settings, get_settings, validate_test_database_path
from app.db import Database


SECRET = "g2-virtual-test-secret-never-for-production"


def set_testing_environment(monkeypatch, root: Path, *, database: bool = True, secret: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SOULSEED_ENVIRONMENT", "testing")
    monkeypatch.setenv("SOULSEED_TEST_DATA_ROOT", str(root.resolve()))
    monkeypatch.setenv("SOULSEED_MODEL_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.delenv("SOULSEED_DATABASE_PATH", raising=False)
    monkeypatch.delenv("SOULSEED_LOCAL_PAIRING_SECRET", raising=False)
    if database:
        monkeypatch.setenv("SOULSEED_DATABASE_PATH", str((root / "app.db").resolve()))
    if secret:
        monkeypatch.setenv("SOULSEED_LOCAL_PAIRING_SECRET", SECRET)
    get_settings.cache_clear()
    return root


def test_testing_requires_explicit_database_path(monkeypatch, tmp_path):
    set_testing_environment(monkeypatch, tmp_path / "case")
    monkeypatch.delenv("SOULSEED_DATABASE_PATH")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="必须显式配置"):
        get_settings()


def test_testing_requires_explicit_pairing_secret(monkeypatch, tmp_path):
    set_testing_environment(monkeypatch, tmp_path / "case")
    monkeypatch.delenv("SOULSEED_LOCAL_PAIRING_SECRET")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="必须显式配置"):
        get_settings()


def test_testing_refuses_formal_database_path_without_opening_it(monkeypatch, tmp_path):
    root = set_testing_environment(monkeypatch, tmp_path / "case")
    monkeypatch.setenv("SOULSEED_DATABASE_PATH", str(FORMAL_DATABASE_PATHS[0]))
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()
    assert not (root / "app.db").exists()


def test_testing_refuses_junction_alias_outside_allowed_root(monkeypatch, tmp_path):
    root = set_testing_environment(monkeypatch, tmp_path / "case" / "allowed")
    outside = tmp_path / "case" / "outside"
    outside.mkdir()
    alias = root / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Windows directory junction creation is unavailable in this account")
    with pytest.raises(ValueError, match="指定测试目录内"):
        validate_test_database_path(alias / "app.db", root)


def test_dotenv_is_ignored_in_testing_and_validation_precedes_sqlite(monkeypatch, tmp_path):
    root = set_testing_environment(monkeypatch, tmp_path / "case")
    dotenv_target = tmp_path / "formal-from-dotenv.db"
    (tmp_path / ".env").write_text(
        f"SOULSEED_DATABASE_PATH={dotenv_target}\nSOULSEED_LOCAL_PAIRING_SECRET={SECRET}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SOULSEED_DATABASE_PATH")
    import sqlite3
    original_connect = sqlite3.connect
    calls = []

    def tracked_connect(*args, **kwargs):
        calls.append(args[0] if args else None)
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="必须显式配置"):
        get_settings()
    assert calls == []
    assert not dotenv_target.exists()
    assert not (root / "app.db").exists()


def test_importing_main_does_not_connect_or_create_test_database(tmp_path):
    root = tmp_path / "import-only"
    root.mkdir()
    database_path = root / "must-not-exist.db"
    script = r"""
import sqlite3
from pathlib import Path
sqlite3.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError('sqlite connected during import'))
import app.main
assert not Path(__import__('os').environ['SOULSEED_DATABASE_PATH']).exists()
print('IMPORT_NO_DATABASE_ACCESS')
"""
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("SOULSEED_")}
    env.update({
        "SOULSEED_ENVIRONMENT": "testing",
        "SOULSEED_TEST_DATA_ROOT": str(root),
        "SOULSEED_DATABASE_PATH": str(database_path),
        "SOULSEED_LOCAL_PAIRING_SECRET": SECRET,
        "SOULSEED_MODEL_BASE_URL": "http://127.0.0.1:9/v1",
    })
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "IMPORT_NO_DATABASE_ACCESS" in result.stdout
    assert not database_path.exists()


def test_module_import_fails_before_any_sqlite_call_when_config_is_missing(tmp_path):
    root = tmp_path / "bad-import"
    root.mkdir()
    script = r"""
import sqlite3
sqlite3.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError('sqlite connected before configuration validation'))
try:
    import app.main
except Exception as exc:
    from pydantic import ValidationError
    assert isinstance(exc, ValidationError), type(exc).__name__
    print('CONFIG_REJECTED_BEFORE_SQLITE')
else:
    raise AssertionError('missing test db path was accepted')
"""
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("SOULSEED_")}
    env.update({
        "SOULSEED_ENVIRONMENT": "testing",
        "SOULSEED_TEST_DATA_ROOT": str(root),
        "SOULSEED_LOCAL_PAIRING_SECRET": SECRET,
        "SOULSEED_MODEL_BASE_URL": "http://127.0.0.1:9/v1",
    })
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "CONFIG_REJECTED_BEFORE_SQLITE" in result.stdout


def test_production_migration_requires_exact_backup_and_confirmation(tmp_path, monkeypatch):
    import app.db as db_module
    import app.config as config_module

    database_path = tmp_path / "virtual-production.db"
    monkeypatch.setattr(config_module, "FORMAL_DATABASE_PATHS", (tmp_path / "virtual-formal.db",))
    all_migrations = db_module.MIGRATIONS
    monkeypatch.setattr(db_module, "MIGRATIONS", {version: sql for version, sql in all_migrations.items() if version <= 13})
    Database(str(database_path), environment="testing", test_data_root=str(tmp_path)).initialize()
    monkeypatch.setattr(db_module, "MIGRATIONS", all_migrations)
    backup_path = tmp_path / "virtual-production-backup.db"
    shutil.copy2(database_path, backup_path)
    before = hashlib.sha256(database_path.read_bytes()).hexdigest()

    with pytest.raises(RuntimeError, match="字节一致的独立备份"):
        Database(
            str(database_path), environment="production",
        ).initialize()
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before

    Database(
        str(database_path), environment="production",
            migration_backup_path=str(backup_path), migration_confirmation="13->18",
    ).initialize()
    with Database(str(database_path), environment="testing", test_data_root=str(tmp_path)).connection() as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 18


def test_test_launcher_fails_on_port_conflict_then_restarts_isolated(tmp_path):
    launcher = Path(__file__).parents[1] / "scripts" / "run_isolated.py"
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        failed = subprocess.run(
            [sys.executable, str(launcher), "api", "--port", str(port)],
            cwd=launcher.parents[2], capture_output=True, text=True, timeout=20,
        )
        assert failed.returncode != 0
        assert "environment=testing database=<test-root>/soulseed-test.db" in failed.stdout
        assert "backend\\data\\soulseed.db" not in (failed.stdout + failed.stderr)

    restarted = subprocess.run(
        [sys.executable, str(launcher), "api", "--port", str(port), "--duration", "3"],
        cwd=launcher.parents[2], capture_output=True, text=True, timeout=20,
    )
    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    assert "environment=testing database=<test-root>/soulseed-test.db" in restarted.stdout
    assert "Uvicorn running on http://127.0.0.1:" in (restarted.stdout + restarted.stderr)


def test_development_default_is_separate_user_data_path(monkeypatch):
    for name in ("SOULSEED_DATABASE_PATH", "SOULSEED_TEST_DATA_ROOT", "SOULSEED_LOCAL_PAIRING_SECRET"):
        monkeypatch.delenv(name, raising=False)
    assert Path(Settings(environment="development").database_path) == DEVELOPMENT_DATABASE_PATH
    assert DEVELOPMENT_DATABASE_PATH not in FORMAL_DATABASE_PATHS


def test_production_requires_an_explicit_non_development_database_path(monkeypatch):
    monkeypatch.delenv("SOULSEED_DATABASE_PATH", raising=False)
    with pytest.raises(ValidationError, match="production 环境必须显式配置数据库路径"):
        Settings(environment="production")


def test_development_uses_its_independent_database_normally(tmp_path, monkeypatch):
    import app.config as config_module

    development_path = tmp_path / "soulseed-development.db"
    monkeypatch.setattr(config_module, "DEVELOPMENT_DATABASE_PATH", development_path)
    monkeypatch.setattr(config_module, "FORMAL_DATABASE_PATHS", (tmp_path / "virtual-formal.db",))
    database = Database(
        str(development_path), environment="development",
    )
    database.initialize()
    with database.connection() as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 18


def test_development_rejects_protected_database_before_sqlite_connect(tmp_path, monkeypatch):
    import app.config as config_module

    protected = tmp_path / "simulated-user-data.db"
    seed = Database(str(protected), environment="testing", test_data_root=str(tmp_path))
    seed.initialize()
    before = hashlib.sha256(protected.read_bytes()).hexdigest()
    monkeypatch.setattr(config_module, "FORMAL_DATABASE_PATHS", (protected,))

    import sqlite3
    calls = []
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: calls.append(a[0] if a else None))
    blocked = Database(str(protected), environment="development")
    with pytest.raises(RuntimeError, match="受保护数据"):
        blocked.initialize()
    with pytest.raises(RuntimeError, match="受保护数据"):
        with blocked.connection():
            pass
    assert calls == []
    assert hashlib.sha256(protected.read_bytes()).hexdigest() == before


def test_development_database_override_cannot_bypass_protection_by_hardlink(tmp_path, monkeypatch):
    import app.config as config_module

    protected = tmp_path / "simulated-user-data.db"
    seed = Database(str(protected), environment="testing", test_data_root=str(tmp_path))
    seed.initialize()
    alias = tmp_path / "friendly-dev-name.db"
    os.link(protected, alias)
    monkeypatch.setattr(config_module, "FORMAL_DATABASE_PATHS", (protected,))

    import sqlite3
    calls = []
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: calls.append(a[0] if a else None))
    with pytest.raises(RuntimeError, match="受保护数据"):
        Database(str(alias), environment="development").initialize()
    assert calls == []


def test_authorized_protected_database_access_is_available_without_migration(tmp_path, monkeypatch):
    import app.config as config_module

    protected = tmp_path / "simulated-user-data.db"
    Database(str(protected), environment="testing", test_data_root=str(tmp_path)).initialize()
    monkeypatch.setattr(config_module, "FORMAL_DATABASE_PATHS", (protected,))
    approved = Database(
        str(protected), environment="development",
        allow_protected_database_access=True,
        protected_database_confirmation=str(protected.resolve()),
    )
    approved.initialize()
    with approved.connection() as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 18
