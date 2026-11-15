from collections.abc import Callable
import asyncio
from time import perf_counter
from dataclasses import dataclass, field
from enum import StrEnum
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta, date
from threading import RLock
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import httpx
from contextlib import nullcontext

from .chat_attempts import active_chat_attempt, chat_attempt_factory

from .config import Settings, validate_test_database_path
from .desktop_runtime import DesktopBudget
from .chat_diagnostics import current_trace, measure
from .model_budget import BudgetRejected, ModelBudgetService
from .ownership import current_character


class ModelConfigurationError(RuntimeError):
    pass


class ModelDailyQuotaExhausted(ModelConfigurationError):
    pass


class ModelQuotaUnavailable(ModelConfigurationError):
    pass


_reservation_lock = RLock()


class ModelRequestError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502, *, message_saved: bool = True,
                 code: str = "model_provider_error"):
        super().__init__(message)
        self.status_code = status_code
        self.message_saved = message_saved
        self.code = code


class ModelTimeoutError(ModelRequestError):
    def __init__(self, kind: str = "read"):
        labels = {"connect": "连接模型服务超时", "read": "等待模型响应超时",
                  "write": "发送模型请求超时", "pool": "等待模型连接超时",
                  "deadline": "模型请求超过后端总等待时限"}
        self.kind = kind
        super().__init__(labels.get(kind, "模型请求超时") + "；当前消息已保存，但没有完整回复。"
                         "系统未自动重试，请勿原样重复提交。上游是否已计费未知。", status_code=504,
                         code="model_timeout")


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


@dataclass(frozen=True)
class ModelBudgetContext:
    """Server-owned identity. Limits belong to the injected budget service."""

    request_id: str
    character_id: str
    source: str
    utc_day: str
    autonomous_reservation_id: str | None = None
    claim_token: str | None = None


@dataclass(frozen=True)
class ActionModelReply:
    """Per-request final output and safe metadata; never exposes reasoning text."""

    content: str | None
    request_id: str | None = None
    finish_reason: str | None = None
    http_status: int | None = None
    usage: dict[str, int] = field(default_factory=dict)
    has_tool_calls: bool = False


class OpenAICompatibleAdapter:
    def __init__(self, settings: Settings, client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
                 *, clock: Callable[[], datetime] | None = None,
                 strict_budget: bool = False, budget_service: ModelBudgetService | None = None,
                 budget_transport: httpx.MockTransport | None = None,
                 budget_audit_path: Path | None = None, legacy_cutover=None):
        isolated_strict = (settings.environment == "testing" and budget_service is not None
                           and budget_service.database.environment == "testing"
                           and type(budget_transport) is httpx.MockTransport)
        development_strict = (settings.environment == "development" and budget_service is not None
                              and budget_service.database.environment == "development"
                              and (budget_transport is None or type(budget_transport) is httpx.MockTransport)
                              and settings.chat_async_real_enabled
                              and settings.model_budget_strict_enabled)
        desktop_strict = (type(budget_service) is DesktopBudget and settings.environment == "production"
                          and settings.database_path == budget_service.database.path
                          and budget_transport is None)
        if strict_budget and not (isolated_strict or development_strict or desktop_strict):
            raise ModelConfigurationError("strict_budget_environment_mismatch")
        self.settings = settings
        self.client_factory = client_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self.strict_budget = strict_budget
        self.desktop_strict = desktop_strict
        self.budget_service = budget_service
        self.budget_transport = budget_transport
        self.legacy_cutover = legacy_cutover
        self.budget_audit_path = None
        if strict_budget and budget_audit_path is not None:
            path = validate_test_database_path(budget_audit_path, budget_service.database.test_data_root)
            if path.suffix.lower() != ".jsonl":
                raise ModelConfigurationError("strict_budget_audit_requires_jsonl")
            self.budget_audit_path = path

    async def reply(self, messages: list[dict[str, str]], *, budget_context: ModelBudgetContext | None = None) -> str:
        content = await self._request_content(messages, budget_context=budget_context)
        return content

    async def reply_contact_plan(self, messages: list[dict[str, str]], *, budget_context: ModelBudgetContext) -> str:
        return await self._request_content(messages, structured=True, budget_context=budget_context)

    async def reply_action(self, messages: list[dict[str, str]], *, run_id: str, step: int,
                           budget_context: ModelBudgetContext | None = None) -> ActionModelReply:
        """JSON content protocol, separate from chat clarification and native tools."""
        return await self._request_completion(
            messages, structured=True,
            request_context={"exploration_run_id": run_id, "exploration_step": step},
            budget_context=budget_context,
        )

    def record_action_validation(self, reply: ActionModelReply, *, run_id: str,
                                 step: int, reason: str | None, shape: dict) -> None:
        # The caller supplies only a schema summary: no user/model text or unknown keys.
        try:
            self._append_log({
                "event": "action_validation", "at": datetime.now(UTC).isoformat(),
                "id": reply.request_id, "exploration_run_id": run_id,
                "exploration_step": step, "reason": reason, "shape": shape,
                "http_status": reply.http_status, "finish_reason": reply.finish_reason,
                "usage": reply.usage, "tool_calls_present": reply.has_tool_calls,
            })
        except OSError:
            # Validation errors are also persisted as bounded task error codes.
            pass

    async def reply_structured(self, messages: list[dict[str, str]], *,
                               budget_context: ModelBudgetContext | None = None) -> StructuredChatReply:
        content = await self._request_content(messages, structured=True, budget_context=budget_context)
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

    async def _request_content(self, messages: list[dict[str, str]], *, structured: bool = False,
                               budget_context: ModelBudgetContext | None = None) -> str:
        reply = await self._request_completion(messages, structured=structured, budget_context=budget_context)
        if structured and reply.finish_reason in {"length", "max_tokens"}:
            raise StructuredOutputError("模型结构化输出达到生成长度上限", code="json_truncated")
        if not isinstance(reply.content, str) or not reply.content.strip():
            raise RuntimeError("模型服务返回了空回复。")
        return reply.content.strip()

    async def _request_completion(self, messages: list[dict[str, str]], *, structured: bool = False,
                                  request_context: dict | None = None,
                                  budget_context: ModelBudgetContext | None = None) -> ActionModelReply:
        factory = chat_attempt_factory.get()
        if factory is None:
            return await self._request_completion_inner(messages, structured=structured,
                                                        request_context=request_context, budget_context=budget_context)
        attempt = factory("structured" if structured else "plain", self.settings.model_max_output_tokens)
        token = active_chat_attempt.set(attempt)
        try:
            if self.strict_budget:
                expected = ModelBudgetContext(attempt.id, attempt.character_id, "chat",
                                              self.clock().astimezone(UTC).date().isoformat())
                if budget_context is not None and budget_context != expected:
                    raise ModelConfigurationError("chat_attempt_budget_binding_mismatch")
                budget_context = expected
            return await self._request_completion_inner(messages, structured=structured,
                                                        request_context=request_context, budget_context=budget_context)
        except BaseException as exc:
            attempt.interrupted(type(exc).__name__)
            raise
        finally:
            active_chat_attempt.reset(token)

    async def _request_completion_inner(self, messages: list[dict[str, str]], *, structured: bool,
                                        request_context: dict | None,
                                        budget_context: ModelBudgetContext | None) -> ActionModelReply:
        if not self.settings.model_enabled:
            raise ModelConfigurationError("真实模型调用已在当前环境关闭。")
        if not self.settings.model_api_key:
            raise ModelConfigurationError("模型 API 密钥未配置。请在 .env 中设置 SOULSEED_MODEL_API_KEY。")
        if not self.settings.model_name:
            raise ModelConfigurationError("模型名称未配置。请在 .env 中设置 SOULSEED_MODEL_NAME。")
        trace = current_trace.get()
        metrics = {"chat_request_id": trace.id if trace else None,
                   "input_chars": sum(len(item.get("content", "")) for item in messages),
                   "message_count": len(messages), "structured": structured,
                   "max_output_tokens": self.settings.model_max_output_tokens,
                   "timeout_seconds": self.settings.model_timeout_seconds,
                   "trust_env": self.settings.model_trust_env}
        metrics.update(request_context or {})
        if self.strict_budget:
            if self.desktop_strict and budget_context is None and request_context is None:
                budget_context = ModelBudgetContext(uuid4().hex, current_character.get(), "chat",
                                                    self.clock().astimezone(UTC).date().isoformat())
            if budget_context is None and self.settings.environment == "development":
                source = "exploration" if request_context and request_context.get("exploration_run_id") else "chat"
                budget_context = ModelBudgetContext(str(uuid4()), current_character.get(), source,
                                                    self.clock().astimezone(UTC).date().isoformat())
            return await self._budgeted_completion(messages, structured, request_context, metrics, budget_context)
        if budget_context is not None:
            raise ModelConfigurationError("budget_context_requires_strict_mode")
        attempt = active_chat_attempt.get()
        admission = self.legacy_cutover.legacy_request() if self.legacy_cutover is not None else nullcontext()
        with admission:
            request_id = self._reserve_request(metrics, request_id=attempt.id if attempt else None)
            return await self._execute_completion(messages, structured, request_context, metrics, request_id)

    async def _budgeted_completion(self, messages, structured, request_context, metrics, context):
        service = self.budget_service
        if type(context) is not ModelBudgetContext or any(
            not isinstance(value, str) or not value.strip()
            for value in (context.request_id, context.character_id, context.source, context.utc_day)
        ):
            raise ModelConfigurationError("trusted_budget_context_required")
        try:
            if date.fromisoformat(context.utc_day).isoformat() != context.utc_day:
                raise ValueError
        except ValueError as exc:
            raise ModelConfigurationError("invalid_budget_window") from exc
        if service.database.in_transaction:
            raise ModelConfigurationError("model_request_inside_transaction")
        if context.source not in {"chat", "exploration", "contact", "autonomous", "draft_experiment", "autonomous_exploration"}:
            raise ModelConfigurationError("unsupported_budget_source")
        if request_context and context.source not in {"exploration", "autonomous_exploration"}:
            raise ModelConfigurationError("exploration_budget_source_required")
        linked = context.source == "autonomous" and self.desktop_strict and bool(
            context.autonomous_reservation_id and context.claim_token)
        if (context.autonomous_reservation_id is not None or context.claim_token is not None or context.source == "autonomous") and not linked:
            raise ModelConfigurationError("d2_joint_lifecycle_not_integrated")
        if context.source == "autonomous_exploration" and not self.desktop_strict:
            raise ModelConfigurationError("strict_budget_environment_mismatch")
        reservation = service.reserve(context.request_id, context.character_id, context.source,
                                      autonomous_reservation_id=context.autonomous_reservation_id,
                                      expected_day=context.utc_day)
        if not reservation.changed:
            raise BudgetRejected("request_already_reserved")
        metrics.update(budget_mode="strict", source=context.source)
        started = False
        settled = False

        def authorize():
            nonlocal started
            result = (service.mark_started(context.request_id, context.character_id, claim_token=context.claim_token)
                      if linked else service.mark_started(context.request_id, context.character_id))
            if not result.changed:
                raise BudgetRejected("request_already_started")
            started = True
            try:
                self._append_log({"event": "started", "day": context.utc_day,
                                  "at": self.clock().isoformat(), "id": context.request_id,
                                  "model": self.settings.model_name, **metrics})
            except OSError:
                pass

        def received(response):
            nonlocal settled
            # Completion here describes HTTP outcome. Business/schema validation follows.
            service.complete(context.request_id, context.character_id, success=response.is_success)
            settled = True

        try:
            return await self._execute_completion(messages, structured, request_context, metrics,
                                                  context.request_id, authorize=authorize, received=received)
        except BaseException:
            if not settled:
                try:
                    if started:
                        service.mark_unknown(context.request_id, context.character_id)
                    else:
                        service.release_unstarted(context.request_id, context.character_id)
                except (BudgetRejected, sqlite3.Error):
                    # A committed started marker cannot be released. Recovery fences it.
                    pass
            raise

    async def _execute_completion(self, messages, structured, request_context, metrics, request_id,
                                  *, authorize=None, received=None):
        url = self.settings.model_base_url.rstrip("/") + "/chat/completions"
        provider = "模型服务" if self.desktop_strict else "硅基流动"
        started = perf_counter()
        attempt = active_chat_attempt.get()
        async def headers_received(_response):
            metrics["response_headers_ms"] = round((perf_counter() - started) * 1000, 3)
        async def request_started(_request):
            if authorize is not None:
                authorize()
            if attempt is not None:
                attempt.sent()
            if request_id and request_context and request_context.get("exploration_run_id"):
                # Runs inside client.post: an HTTP attempt, not proof of receipt/billing.
                try:
                    self._append_log({"event": "http_started", "at": self.clock().isoformat(),
                                      "id": request_id, **request_context})
                except OSError as exc:
                    if authorize is not None:
                        return
                    raise ModelQuotaUnavailable("无法持久记录探索请求，已暂停探索。") from exc
        def record(status, response=None):
            metrics["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
            self._record_result(request_id, status, response, metrics=metrics)
        try:
            # HTTPX timeouts bound inactivity per phase; also bound total elapsed time.
            with measure("model_http"):
                factory = self.client_factory
                options = {}
                if authorize is not None:
                    # A single MockTransport attempt, no custom factory, redirects or retries.
                    factory = httpx.AsyncClient
                    options = {"transport": self.budget_transport, "follow_redirects": False}
                async with asyncio.timeout(self.settings.model_timeout_seconds), factory(
                    timeout=self.settings.model_timeout_seconds, trust_env=self.settings.model_trust_env,
                    event_hooks={"request": [request_started], "response": [headers_received]},
                    **options,
                ) as client:
                    body = {"model": self.settings.model_name, "messages": messages}
                    max_tokens = attempt.max_output_tokens if attempt else self.settings.model_max_output_tokens
                    if max_tokens is not None:
                        body["max_tokens"] = max_tokens
                    if structured:
                        body["response_format"] = {"type": "json_object"}
                    response = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {self.settings.model_api_key}"},
                        json=body,
                    )
        except httpx.TimeoutException as exc:
            kind = {httpx.ConnectTimeout: "connect", httpx.ReadTimeout: "read",
                    httpx.WriteTimeout: "write", httpx.PoolTimeout: "pool"}.get(type(exc), "read")
            record("timeout_" + kind)
            raise ModelTimeoutError(kind) from exc
        except TimeoutError as exc:
            record("timeout_deadline")
            raise ModelTimeoutError("deadline") from exc
        except httpx.RequestError as exc:
            record("network_error")
            raise ModelRequestError(f"无法连接{provider}，请检查网络或稍后重试。", code="model_network_error") from exc
        if attempt is not None:
            provider_id = response.headers.get("x-request-id")
            attempt.responded(response.status_code, provider_id[:128] if provider_id and provider_id.isascii() else None)
        if received is not None:
            received(response)
        record(f"http_{response.status_code}", response)
        if response.is_error or (authorize is not None and not response.is_success):
            status = response.status_code
            if status in (400, 413) and self._is_context_limit_response(response):
                raise ModelRequestError(
                    "模型服务报告输入超过该模型当前上下文限制。当前消息已保存但未生成回复；"
                    "请缩短对话上下文后继续，不要原样重复提交。",
                    status_code=413,
                )
            if status in (401, 403):
                raise ModelRequestError(f"{provider}的 API Key 无效、已失效或当前账号无权调用该模型。", status_code=502)
            if status == 404:
                raise ModelRequestError(f"{provider}找不到模型，请核对模型 ID。", status_code=502)
            if status == 429:
                raise ModelRequestError(f"{provider}请求受到限流或额度限制，请检查账户余额、配额和速率限制。", status_code=429, code="model_provider_rate_limited")
            if status in (500, 502, 503, 504):
                raise ModelRequestError(f"{provider}返回 HTTP {status}；当前消息已保存但未获得回复，系统未自动重试。", status_code=502)
            raise ModelRequestError(f"{provider}请求失败（HTTP {status}）。", status_code=502)
        try:
            # SiliconFlow may also return reasoning_content; only final content is user-visible.
            payload = response.json()
            choice = payload["choices"][0]
            message = choice["message"]
            content = message.get("content")
            usage = self._safe_usage(payload.get("usage"))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise RuntimeError("模型服务返回了无法识别的响应。") from exc
        finish = choice.get("finish_reason")
        return ActionModelReply(
            content=content if isinstance(content, str) else None,
            request_id=request_id, http_status=response.status_code, usage=usage,
            finish_reason=finish if finish is None or isinstance(finish, str) and finish in {"stop", "length", "max_tokens", "tool_calls", "content_filter"} else "unknown",
            has_tool_calls=bool(message.get("tool_calls") or message.get("function_call")),
        )

    @staticmethod
    def _safe_usage(raw) -> dict[str, int]:
        if not isinstance(raw, dict):
            return {}
        usage = {key: raw[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                 if type(raw.get(key)) is int and raw[key] >= 0}
        details = raw.get("completion_tokens_details")
        if isinstance(details, dict) and type(details.get("reasoning_tokens")) is int and details["reasoning_tokens"] >= 0:
            usage["reasoning_tokens"] = details["reasoning_tokens"]
        return usage

    @staticmethod
    def _is_context_limit_response(response: httpx.Response) -> bool:
        """Recognize provider context errors without exposing its raw response body."""
        try:
            body = response.json()
        except (ValueError, TypeError):
            return False
        candidates: list[str] = []
        if isinstance(body, dict):
            for key in ("message", "detail", "code", "type", "error"):
                value = body.get(key)
                if isinstance(value, str):
                    candidates.append(value)
                elif isinstance(value, dict):
                    candidates.extend(
                        item for item in value.values() if isinstance(item, str)
                    )
        diagnostic = " ".join(candidates).casefold()
        markers = (
            "context length", "context_length", "maximum context", "max context",
            "prompt is too long", "too many tokens", "token limit", "上下文长度", "上下文超限",
        )
        return any(marker in diagnostic for marker in markers)

    def _append_log(self, record: dict) -> None:
        path = self.budget_audit_path if self.strict_budget else self.settings.model_request_log_path
        if not path:
            return
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        # The local instance lock allows one backend process for this database.
        # A started reservation is never refunded, including after a crash.
        with _reservation_lock, target.open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            log.flush()
            os.fsync(log.fileno())

    def _quota_records(self) -> list[dict]:
        """Read only, fail closed: an absent/corrupt ledger is not an empty ledger."""
        path = self.budget_audit_path if self.strict_budget else self.settings.model_request_log_path
        if not path:
            raise ModelQuotaUnavailable("探索额度记录未配置，已暂停探索。")
        try:
            with Path(path).expanduser().open("r", encoding="utf-8") as log:
                rows = [json.loads(line) for line in log if line.strip()]
            ids = set()
            for row in rows:
                if not isinstance(row, dict) or row.get("event") not in {"started", "finished", "action_validation", "http_started"}:
                    raise ValueError("invalid ledger record")
                if not isinstance(row.get("id"), str) or not row["id"]:
                    raise ValueError("missing request id")
                if row["event"] == "started":
                    if date.fromisoformat(row["day"]).isoformat() != row["day"]:
                        raise ValueError("invalid reservation day")
                    if row["id"] in ids:
                        raise ValueError("duplicate reservation")
                    ids.add(row["id"])
                elif row["id"] not in ids:
                    raise ValueError("orphaned request record")
            return rows
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ModelQuotaUnavailable("无法可靠读取探索额度记录，已暂停探索。") from exc

    def quota_snapshot(self, *, now: datetime | None = None) -> dict:
        if self.strict_budget:
            raise ModelConfigurationError("strict_budget_usage_requires_sqlite_snapshot")
        current = (now or self.clock()).astimezone(UTC)
        reset = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        result = {"limit": self.settings.model_daily_request_limit, "used": None,
                  "remaining": None, "resets_at": reset.isoformat(), "day": current.date().isoformat(),
                  "available": False, "reason": "exploration_quota_unavailable"}
        with _reservation_lock:
            try:
                rows = self._quota_records()
            except ModelQuotaUnavailable:
                return result
            limit = result["limit"]
            if limit is None:
                return result
            used = sum(row["event"] == "started" and row.get("day") == result["day"] for row in rows)
            result.update(used=used, remaining=max(0, limit - used), available=used < limit,
                          reason=None if used < limit else "exploration_daily_quota_exhausted")
        return result

    def request_diagnostics(self, run_id: str) -> dict:
        result = {"reservations": None, "http_attempts": None, "http_attempts_complete": False,
                  "provider_receipt": "unknown", "provider_billing": "unknown"}
        with _reservation_lock:
            try:
                rows = [row for row in self._quota_records() if row.get("exploration_run_id") == run_id]
            except ModelQuotaUnavailable:
                return result
        reserved = {row["id"] for row in rows if row["event"] == "started"}
        marked = {row["id"] for row in rows if row["event"] == "http_started"}
        # Historical HTTP responses prove an attempt, but a missing finish record
        # cannot establish whether a legacy reservation reached the transport.
        proven = marked | {row["id"] for row in rows if row["event"] == "finished" and str(row.get("status", "")).startswith("http_")}
        result.update(reservations=len(reserved), http_attempts=len(proven),
                      http_attempts_complete=reserved <= proven)
        return result

    def _reserve_request(self, metrics: dict | None = None, *, request_id: str | None = None) -> str | None:
        with _reservation_lock:
            return self._reserve_request_locked(metrics, request_id=request_id)

    def _reserve_request_locked(self, metrics: dict | None = None, *, request_id: str | None = None) -> str | None:
        path = self.settings.model_request_log_path
        limit = self.settings.model_daily_request_limit
        exploration = bool(metrics and metrics.get("exploration_run_id"))
        if (limit is not None or exploration) and not path:
            if exploration:
                raise ModelQuotaUnavailable("探索额度记录未配置，已暂停探索。")
            raise ModelConfigurationError("模型请求限额需要持久化调用记录路径。")
        if not path:
            return None
        now = self.clock().astimezone(UTC)
        day = now.date().isoformat()
        target = Path(path).expanduser()
        if exploration:
            snapshot = self.quota_snapshot(now=now)
            if snapshot["reason"] == "exploration_quota_unavailable":
                raise ModelQuotaUnavailable("无法可靠读取探索额度记录，已暂停探索。")
            if not snapshot["available"]:
                raise ModelDailyQuotaExhausted("今日探索模型请求预留次数已达到上限。")
        elif limit is not None and target.exists():
            try:
                with target.open("r", encoding="utf-8") as log:
                    used = sum(1 for line in log if line.startswith('{"event":"started","day":"' + day + '"'))
            except OSError as exc:
                raise ModelConfigurationError("无法读取模型调用记录；已拒绝本次请求。") from exc
            if used >= limit:
                raise ModelDailyQuotaExhausted("今日模型请求次数已达到开发环境上限。")
        request_id = request_id or str(uuid4())
        try:
            self._append_log({"event": "started", "day": day, "at": now.isoformat(),
                              "id": request_id, "model": self.settings.model_name, **(metrics or {})})
        except OSError as exc:
            if exploration:
                raise ModelQuotaUnavailable("无法持久记录探索请求，已暂停探索。") from exc
            raise ModelConfigurationError("无法持久记录模型请求；已拒绝本次请求。") from exc
        return request_id

    def _record_result(self, request_id: str | None, status: str, response: httpx.Response | None = None, *, metrics: dict | None = None) -> None:
        if request_id is None:
            return
        usage = None
        output_sizes = {}
        if response is not None:
            try:
                payload = response.json()
                usage = self._safe_usage(payload.get("usage"))
                choice = payload.get("choices", [{}])[0]
                message = choice.get("message", {})
                output_sizes["tool_calls_present"] = bool(message.get("tool_calls") or message.get("function_call"))
                for field in ("content", "reasoning_content"):
                    if isinstance(message.get(field), str):
                        output_sizes[field + "_chars"] = len(message[field])
                if choice.get("finish_reason") in {"stop", "length", "max_tokens", "tool_calls", "content_filter"}:
                    output_sizes["finish_reason"] = choice["finish_reason"]
            except (ValueError, AttributeError, TypeError, IndexError):
                pass
        try:
            self._append_log({"event": "finished", "at": self.clock().isoformat(),
                              "id": request_id, "status": status, "usage": usage, **(metrics or {}), **output_sizes})
        except OSError:
            # Reservation remains counted. A log write failure never causes a retry.
            pass

