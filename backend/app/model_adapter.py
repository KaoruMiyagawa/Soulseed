from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import json
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


class StructuredOutputError(RuntimeError):
    """Safe structured-output failure with a diagnostic code, never raw model text."""

    def __init__(self, message: str, *, code: str = "invalid_structure"):
        super().__init__(message)
        self.code = code


class ClarificationAction(StrEnum):
    ASK = "ask"
    DEFER = "defer"
    NORMAL = "normal"


class ClarificationResolution(StrEnum):
    NONE = "none"
    EXPLICIT_CHANGE = "explicit_change"
    ORIGINAL_WAS_WRONG = "original_was_wrong"
    COEXISTING = "coexisting"
    DENIED = "denied"
    TEMPORARY = "temporary"
    UNCLEAR = "unclear"
    OFF_TOPIC = "off_topic"


@dataclass(frozen=True)
class StructuredChatReply:
    content: str
    clarification_id: str | None
    action: ClarificationAction
    resolution: ClarificationResolution
    evidence_excerpt: str | None
    ignored_extra_fields: bool = False


class ModelAdapter(Protocol):
    async def reply(self, messages: list[dict[str, str]]) -> str: ...


class OpenAICompatibleAdapter:
    def __init__(self, settings: Settings, client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient):
        self.settings = settings
        self.client_factory = client_factory

    async def reply(self, messages: list[dict[str, str]]) -> str:
        content = await self._request_content(messages)
        return content

    async def reply_structured(self, messages: list[dict[str, str]]) -> StructuredChatReply:
        content = await self._request_content(messages, structured=True)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            tail = content.rstrip()
            truncated = (
                exc.pos >= max(0, len(tail) - 2)
                and exc.msg in {
                    "Expecting value",
                    "Expecting ',' delimiter",
                    "Expecting property name enclosed in double quotes",
                    "Unterminated string starting at",
                }
            )
            raise StructuredOutputError(
                "模型结构化输出不完整" if truncated else "模型结构化输出不是有效 JSON",
                code="json_truncated" if truncated else "json_syntax_error",
            ) from exc
        if not isinstance(payload, dict):
            raise StructuredOutputError("结构化输出顶层必须是对象", code="top_level_not_object")
        missing = {"reply", "clarification"} - payload.keys()
        if missing:
            raise StructuredOutputError("结构化输出缺少必需字段", code="missing_top_level_fields")

        reply = payload["reply"]
        decision = payload["clarification"]
        if not isinstance(reply, str):
            raise StructuredOutputError("reply 字段类型无效", code="invalid_reply_type")
        if not reply.strip():
            raise StructuredOutputError("reply 字段为空", code="empty_reply")
        if len(reply) > 20000:
            raise StructuredOutputError("reply 字段超过长度限制", code="reply_too_long")
        if not isinstance(decision, dict):
            raise StructuredOutputError("clarification 字段必须是对象", code="clarification_not_object")
        missing_decision = {"clarification_id", "action", "resolution", "evidence_excerpt"} - decision.keys()
        if missing_decision:
            raise StructuredOutputError("clarification 对象缺少必需字段", code="missing_clarification_fields")

        clarification_id = decision["clarification_id"]
        evidence_excerpt = decision["evidence_excerpt"]
        if clarification_id is not None and not isinstance(clarification_id, str):
            raise StructuredOutputError("clarification_id 类型无效", code="invalid_clarification_id_type")
        if evidence_excerpt is not None and not isinstance(evidence_excerpt, str):
            raise StructuredOutputError("evidence_excerpt 类型无效", code="invalid_evidence_excerpt_type")
        try:
            action = ClarificationAction(decision["action"])
        except (TypeError, ValueError) as exc:
            raise StructuredOutputError("action 不属于支持的枚举值", code="invalid_action_enum") from exc
        try:
            resolution = ClarificationResolution(decision["resolution"])
        except (TypeError, ValueError) as exc:
            raise StructuredOutputError("resolution 不属于支持的枚举值", code="invalid_resolution_enum") from exc

        return StructuredChatReply(
            content=reply.strip(),
            clarification_id=clarification_id,
            action=action,
            resolution=resolution,
            evidence_excerpt=evidence_excerpt,
            ignored_extra_fields=bool(
                payload.keys() - {"reply", "clarification"}
                or decision.keys() - {"clarification_id", "action", "resolution", "evidence_excerpt"}
            ),
        )

    async def _request_content(self, messages: list[dict[str, str]], *, structured: bool = False) -> str:
        if not self.settings.model_api_key:
            raise ModelConfigurationError("模型 API 密钥未配置。请在 .env 中设置 SOULSEED_MODEL_API_KEY。")
        if not self.settings.model_name:
            raise ModelConfigurationError("模型名称未配置。请在 .env 中设置 SOULSEED_MODEL_NAME。")
        url = self.settings.model_base_url.rstrip("/") + "/chat/completions"
        try:
            async with self.client_factory(timeout=self.settings.model_timeout_seconds) as client:
                body = {"model": self.settings.model_name, "messages": messages}
                if structured:
                    body["response_format"] = {"type": "json_object"}
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.settings.model_api_key}"},
                    json=body,
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
            choice = response.json()["choices"][0]
            if structured and choice.get("finish_reason") in {"length", "max_tokens"}:
                raise StructuredOutputError("模型结构化输出达到生成长度上限", code="json_truncated")
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("模型服务返回了无法识别的响应。") from exc
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("模型服务返回了空回复。")
        return content.strip()

