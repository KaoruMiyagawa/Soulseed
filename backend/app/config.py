from __future__ import annotations

import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEVELOPMENT_DATABASE_PATH = PROJECT_ROOT / "backend" / "data" / "soulseed-development.db"
FORMAL_DATABASE_PATHS = (
    PROJECT_ROOT / "backend" / "data" / "soulseed.db",
    PROJECT_ROOT / "frontend" / "backend" / "data" / "soulseed.db",
)


def _contained(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_test_database_path(database_path: str | Path, test_data_root: str | Path) -> Path:
    """Resolve and fence a testing database before any SQLite connection exists."""
    root_input = Path(test_data_root).expanduser()
    database_input = Path(database_path).expanduser()
    if not root_input.is_absolute() or not database_input.is_absolute():
        raise ValueError("测试目录和数据库路径必须是绝对路径")

    try:
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        root = root_input.resolve(strict=True)
        if not root.is_dir() or root == temp_root or not _contained(root, temp_root):
            raise ValueError("测试目录必须是系统临时目录下独立存在的目录")
        if _contained(root, PROJECT_ROOT.resolve(strict=True)):
            raise ValueError("测试目录不能位于项目仓库内")

        parent = database_input.parent.resolve(strict=True)
        candidate = parent / database_input.name
        # Reject paths outside the disposable root before checking whether the
        # target file exists. In particular, never probe the formal DB path.
        if not _contained(candidate, root):
            raise ValueError("测试数据库解析后必须位于指定测试目录内")
        if database_input.is_symlink():
            raise ValueError("测试数据库不能是符号链接")
        if candidate.exists():
            candidate = candidate.resolve(strict=True)
            if not candidate.is_file():
                raise ValueError("测试数据库路径不是普通文件")
        else:
            candidate = candidate.resolve(strict=False)

        if not _contained(candidate, root):
            raise ValueError("测试数据库解析后必须位于指定测试目录内")
        return candidate
    except (OSError, RuntimeError) as exc:
        raise ValueError("无法安全解析测试目录或数据库路径") from exc


class Settings(BaseSettings):
    environment: Literal["testing", "development", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = 8001
    database_path: str = str(DEVELOPMENT_DATABASE_PATH)
    test_data_root: str | None = Field(default=None, repr=False)
    test_frontend_origin: str | None = Field(default=None, repr=False)
    development_frontend_origin: str | None = None
    model_base_url: str = "https://api.siliconflow.cn/v1"
    model_api_key: str | None = Field(default=None, repr=False)
    model_name: str = "deepseek-ai/DeepSeek-V4.1-Flash"
    model_timeout_seconds: float = 60
    model_enabled: bool = True
    model_max_output_tokens: int | None = Field(default=None, ge=64, le=4096)
    model_daily_request_limit: int | None = Field(default=None, ge=1, le=1000)
    model_conversation_turn_limit: int | None = Field(default=None, ge=1, le=1000)
    model_request_log_path: str | None = None
    system_prompt: str | None = None
    memory_context_max_chars: int = 4000
    memory_context_max_items: int = 5
    memory_item_max_chars: int = 600
    life_timezone: str = "Asia/Shanghai"
    life_background_enabled: bool = True
    life_background_interval_minutes: int = Field(default=30, ge=5)
    contact_enabled: bool = True
    contact_model_generation_enabled: bool = False
    contact_background_enabled: bool = True
    contact_check_interval_minutes: int = Field(default=60, ge=30)
    local_pairing_secret: str | None = Field(default=None, repr=False, min_length=32)
    migration_backup_path: str | None = Field(default=None, repr=False)
    migration_confirmation: str | None = Field(default=None, repr=False)
    allow_protected_database_access: bool = False
    protected_database_confirmation: str | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_environment(self) -> "Settings":
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("当前身份方案仅支持本地回环地址")

        if self.environment == "production":
            if "database_path" not in self.model_fields_set:
                raise ValueError("production 环境必须显式配置数据库路径")
            selected_database = Path(self.database_path).expanduser().resolve(strict=False)
            development_database = DEVELOPMENT_DATABASE_PATH.resolve(strict=False)
            if selected_database == development_database:
                raise ValueError("production 环境不能使用 development 数据库路径")

        if self.development_frontend_origin:
            frontend = urlsplit(self.development_frontend_origin)
            if (self.environment != "development" or frontend.scheme != "http"
                    or frontend.hostname not in {"127.0.0.1", "localhost"}
                    or frontend.port is None or frontend.path not in {"", "/"}
                    or frontend.username or frontend.password or frontend.query or frontend.fragment):
                raise ValueError("development 前端来源必须是明确的本机 HTTP 地址和端口")

        if self.environment == "testing":
            required = ("database_path", "test_data_root", "local_pairing_secret")
            missing = [name for name in required if name not in self.model_fields_set]
            if missing:
                raise ValueError("testing 环境必须显式配置测试数据库路径、测试目录和测试口令")
            if not self.local_pairing_secret:
                raise ValueError("testing 环境必须配置非空测试口令")
            resolved = validate_test_database_path(self.database_path, self.test_data_root)
            object.__setattr__(self, "database_path", str(resolved))
            parsed = urlsplit(self.model_base_url)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("testing 环境只允许配置本机模型端点")
            if self.test_frontend_origin:
                frontend = urlsplit(self.test_frontend_origin)
                if (frontend.scheme != "http" or frontend.hostname not in {"127.0.0.1", "localhost"}
                        or frontend.port is None or frontend.path not in {"", "/"}
                        or frontend.username or frontend.password or frontend.query or frontend.fragment):
                    raise ValueError("testing 前端来源必须是明确的本机 HTTP 地址和端口")

        return self

    model_config = SettingsConfigDict(env_prefix="SOULSEED_", env_file=None, extra="ignore")


@lru_cache
def get_settings() -> Settings:
    # Testing can only be selected by the process environment. It never asks
    # pydantic-settings to read the project .env file.
    environment = os.environ.get("SOULSEED_ENVIRONMENT", "development")
    dotenv = None if environment == "testing" else ".env"
    return Settings(environment=environment, _env_file=dotenv)
