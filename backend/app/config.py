from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    host: str = "127.0.0.1"
    port: int = 8001
    database_path: str = "backend/data/soulseed.db"
    model_base_url: str = "https://api.siliconflow.cn/v1"
    model_api_key: str | None = Field(default=None, repr=False)
    model_name: str = "deepseek-ai/DeepSeek-V4.1-Flash"
    model_timeout_seconds: float = 60
    system_prompt: str | None = None
    memory_context_max_chars: int = 4000
    memory_context_max_items: int = 5
    memory_item_max_chars: int = 600

    model_config = SettingsConfigDict(env_prefix="SOULSEED_", env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

