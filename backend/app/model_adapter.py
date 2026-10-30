from collections.abc import Callable
from typing import Protocol

import httpx

from .config import Settings


class ModelConfigurationError(RuntimeError):
    pass


class ModelRequestError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class ModelTimeoutError(ModelRequestError):
    def __init__(self):
        super().__init__("硅基流动模型请求超时，请稍后重试。", status_code=504)


class ModelAdapter(Protocol):
    async def reply(self, messages: list[dict[str, str]]) -> str: ...


class OpenAICompatibleAdapter:
    def __init__(self, settings: Settings, client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient):
        self.settings = settings
        self.client_factory = client_factory

    async def reply(self, messages: list[dict[str, str]]) -> str:
        if not self.settings.model_api_key:
            raise ModelConfigurationError("模型 API 密钥未配置。请在 .env 中设置 SOULSEED_MODEL_API_KEY。")
        if not self.settings.model_name:
            raise ModelConfigurationError("模型名称未配置。请在 .env 中设置 SOULSEED_MODEL_NAME。")
        url = self.settings.model_base_url.rstrip("/") + "/chat/completions"
        try:
            async with self.client_factory(timeout=self.settings.model_timeout_seconds) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.settings.model_api_key}"},
                    json={"model": self.settings.model_name, "messages": messages},
                )
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError() from exc
        except httpx.RequestError as exc:
            raise ModelRequestError("无法连接硅基流动模型服务，请检查网络或稍后重试。") from exc
        if response.is_error:
            status = response.status_code
            if status in (401, 403):
                raise ModelRequestError("硅基流动 API Key 无效、已失效或当前账号无权调用该模型。", status_code=502)
            if status == 404:
                raise ModelRequestError(f"硅基流动找不到模型 {self.settings.model_name}，请核对模型 ID。", status_code=502)
            if status == 429:
                raise ModelRequestError("硅基流动请求受到限流或额度限制，请检查账户余额、配额和速率限制。", status_code=429)
            if status in (500, 502, 503, 504):
                raise ModelRequestError("硅基流动模型服务暂时不可用，请稍后重试。", status_code=502)
            raise ModelRequestError(f"硅基流动模型请求失败（HTTP {status}）。", status_code=502)
        try:
            # SiliconFlow may also return reasoning_content; only final content is user-visible.
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("模型服务返回了无法识别的响应。") from exc
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("模型服务返回了空回复。")
        return content.strip()

