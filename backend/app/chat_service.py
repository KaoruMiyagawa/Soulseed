from __future__ import annotations

import json
import logging
import re

from .expression import ExpressionContextBuilder
from .context_window import ContextBudgetExceeded, ModelContextWindow
from .chat_diagnostics import checkpoint, count, measure
from .memory.context_builder import MemoryContextBuilder
from .memory.correction_flow_service import CorrectionFlowResult, CorrectionFlowService, CorrectionFlowStatus
from .memory.clarification_repository import (
    ClarificationRecord,
    ClarificationRepository,
    ClarificationStateError,
    ClarificationStatus,
)
from .memory.extractor import DeterministicMemoryExtractor
from .memory.models import CorrectionIntent
from .memory.retriever import MemoryRetriever
from .memory.service import MemoryService
from .model_adapter import (
    ClarificationAction,
    ClarificationResolution,
    ModelAdapter,
    ModelConfigurationError,
    ModelRequestError,
    StructuredChatReply,
    StructuredOutputError,
)
from .memory.clarification_application_service import ClarificationApplicationService
from .memory.correction_service import MemoryCorrectionService, ProposalApplicationError
from .memory.models import ProposalStatus
from .memory.proposal_repositories import ProposalNotFoundError, ProposalStateError
from .memory.repositories import (
    DuplicateMemoryError,
    IdempotencyConflictError,
    MemoryNotFoundError,
    MessageNotFoundError,
)
from .memory.proposal_service import MemoryProposalService
from .repositories import ConversationRepository, MessageRepository
from .ownership import current_character

logger = logging.getLogger(__name__)


class ChatService:
    def __init__(
        self,
        conversations: ConversationRepository,
        messages: MessageRepository,
        model: ModelAdapter,
        system_prompt: str,
        memory_service: MemoryService | None = None,
        memory_extractor: DeterministicMemoryExtractor | None = None,
        memory_retriever: MemoryRetriever | None = None,
        memory_context_builder: MemoryContextBuilder | None = None,
        correction_flow: CorrectionFlowService | None = None,
        clarification_repository: ClarificationRepository | None = None,
        proposal_service: MemoryProposalService | None = None,
        clarification_application: ClarificationApplicationService | None = None,
        memory_correction_service: MemoryCorrectionService | None = None,
        life_repository=None,
        contact_service=None,
        context_window: ModelContextWindow | None = None,
        runtime_features: dict[str, bool] | None = None,
    ):
        self.conversations, self.messages, self.model, self.system_prompt = conversations, messages, model, system_prompt
        self.memory_service, self.memory_extractor = memory_service, memory_extractor
        self.memory_retriever, self.memory_context_builder = memory_retriever, memory_context_builder
        self.correction_flow = correction_flow
        self.clarifications = clarification_repository
        self.proposal_service = proposal_service
        self.clarification_application = clarification_application
        self.memory_correction_service = memory_correction_service
        self.life_repository = life_repository
        self.contact_service = contact_service
        self.expression_context_builder = ExpressionContextBuilder()
        self.context_window = context_window or ModelContextWindow()
        self.runtime_features = runtime_features or {}
        self.last_context_report = None

    async def send(self, conversation_id: str, content: str) -> dict:
        checkpoint("user_persistence")
        self.conversations.get(conversation_id)
        # Reject an intrinsically oversized input before writing it. This makes
        # a visible 413 safe to correct and retry without creating duplicate turns.
        self.context_window.validate_current_message(content)
        user_message = self.messages.add(conversation_id, "user", content)
        checkpoint("contact_boundary")
        if self.contact_service is not None:
            try:
                self.contact_service.observe_user_message(content, character_id=current_character.get())
            except Exception:
                logger.exception("主动联系偏好更新失败 message_id=%s", user_message["id"])

        checkpoint("clarification_lookup")
        pending: ClarificationRecord | None = None
        claim = None
        clarification_busy = False
        if self.clarifications is not None:
            try:
                pending = self.clarifications.find_active_for_conversation(conversation_id)
                if pending is not None:
                    if pending.status is ClarificationStatus.PREPARING:
                        clarification_busy = True
                    else:
                        claim = self.clarifications.claim_response(pending.id, user_message["id"])
                        if not claim.acquired:
                            clarification_busy = True
            except ClarificationStateError:
                # Never choose between multiple clarifications or steal a live lease.
                clarification_busy = True
                logger.info("澄清答复未关联，已有其他处理或多个待处理事项 message_id=%s", user_message["id"])
            except Exception:
                clarification_busy = True
                logger.exception("读取或认领澄清状态失败 message_id=%s", user_message["id"])

        if claim is not None and claim.acquired:
            assistant_message = await self._continue_clarification(
                conversation_id, user_message, claim.clarification, claim.clarification.processing_token
            )
            self.conversations.touch(conversation_id)
            return {**assistant_message, "correction": self._correction_summary(())}

        checkpoint("memory_retrieval")
        memory_context = ""
        memory_matches = []
        share_service = None
        share_reservation = None
        if self.memory_retriever and self.memory_context_builder:
            try:
                memory_matches = self.memory_retriever.retrieve(content)
                memory_context = self.memory_context_builder.build(memory_matches)
            except Exception:
                logger.exception("记忆检索或上下文组装失败，继续执行聊天请求 message_id=%s", user_message["id"])

        checkpoint("memory_extraction")
        correction_results: tuple[CorrectionFlowResult, ...] = ()
        if not clarification_busy and self.memory_service and self.memory_extractor:
            candidates = []
            try:
                candidates = self.memory_extractor.extract(user_message)
            except Exception:
                logger.exception("记忆提取失败，继续执行聊天请求 message_id=%s", user_message["id"])
            else:
                for candidate in candidates:
                    if candidate.intent is CorrectionIntent.NONE:
                        try:
                            self.memory_service.persist_candidate(candidate, user_message)
                        except Exception:
                            logger.exception("普通记忆持久化失败，继续执行聊天请求 message_id=%s", user_message["id"])
                correction_candidates = tuple(candidate for candidate in candidates if candidate.intent is not CorrectionIntent.NONE)
                if correction_candidates:
                    if self.correction_flow is None:
                        correction_results = tuple(
                            CorrectionFlowResult(
                                status=CorrectionFlowStatus.PROCESSING_FAILED,
                                reason="纠正流程服务未配置",
                                error_code="correction_flow_unavailable",
                            )
                            for _ in correction_candidates
                        )
                    else:
                        try:
                            correction_results = tuple(self.correction_flow.process(user_message, correction_candidates))
                        except Exception:
                            logger.exception("纠正流程发生未预期异常，继续执行聊天请求 message_id=%s", user_message["id"])
                            correction_results = tuple(
                                CorrectionFlowResult(
                                    status=CorrectionFlowStatus.PROCESSING_FAILED,
                                    reason="纠正流程发生未预期异常",
                                    error_code="correction_flow_unexpected_error",
                                )
                                for _ in correction_candidates
                            )

        checkpoint("life_expression")
        # Capability facts are independent of the life-history snapshot. Keep
        # them available even if life advancement or expression lookup fails.
        runtime_capabilities = self._runtime_capabilities(content, current_character.get())
        if self.life_repository is not None:
            try:
                from .life_engine import LifeEngine
                LifeEngine(self.life_repository).advance(current_character.get())
                snapshot = self.life_repository.expression_snapshot(content, character_id=current_character.get())
                if not correction_results and not clarification_busy:
                    from .life_share import LifeShareService
                    share_service = LifeShareService(self.life_repository)
                    share_reservation = share_service.reserve_for_chat(content, character_id=current_character.get())
                continues_topic = any(word in content for word in ("刚才", "上次", "之前说", "接着", "继续聊", "还记得"))
                history = self.messages.list_for_conversation(conversation_id) if continues_topic else []
                recent = ([f"{item['role']}: {item['content']}" for item in history[-4:-1]
                           if item.get("role") in {"user", "assistant"}] if continues_topic else [])
                expression = self.expression_context_builder.build(
                    character_id=current_character.get(), intent=content,
                    memory_matches=memory_matches, life_events=snapshot["events"],
                    current_activity=snapshot["current_activity"],
                    share=(share_reservation or {}).get("event"),
                    preferences=snapshot["preferences"], recent_interactions=recent,
                    boundaries=(self.contact_service.expression_boundaries(current_character.get())
                                if self.contact_service is not None and "proactive_contact" in runtime_capabilities else ()),
                    local_date=snapshot["local_date"], timezone_id=snapshot["timezone_id"],
                    life_scope=snapshot["life_scope"],
                    runtime_capabilities=runtime_capabilities,
                )
                expression_message = expression.to_system_message()
                memory_context = ""
                if expression_message:
                    memory_context = expression_message["content"]
            except Exception:
                logger.exception("生活状态读取或推进失败，继续执行聊天请求 message_id=%s", user_message["id"])
                expression_message = self.expression_context_builder.build(
                    character_id=current_character.get(), intent=content,
                    memory_matches=memory_matches,
                    runtime_capabilities=runtime_capabilities,
                ).to_system_message()
                memory_context = expression_message["content"] if expression_message else (
                    self.memory_context_builder.build(memory_matches) if self.memory_context_builder else ""
                )
        elif memory_matches:
            expression_message = self.expression_context_builder.build(
                character_id=current_character.get(), intent=content, memory_matches=memory_matches,
            ).to_system_message()
            memory_context = expression_message["content"] if expression_message else ""
        else:
            if runtime_capabilities:
                expression_message = self.expression_context_builder.build(
                    character_id=current_character.get(), intent=content,
                    runtime_capabilities=runtime_capabilities,
                ).to_system_message()
                memory_context = expression_message["content"] if expression_message else ""
        checkpoint("history_context")
        model_messages = self._model_messages(conversation_id, memory_context)
        clarification = self._single_clarification_context(correction_results)
        if clarification is not None and self.clarifications is not None:
            record = None
            try:
                record = self.clarifications.create(
                    conversation_id,
                    user_message["id"],
                    clarification["context"],
                    source_proposal_id=clarification["source_proposal_id"],
                )
                model_messages.append({"role": "system", "content": self._clarification_context_message(record)})
                answer = await self._structured_generation(
                    model_messages,
                    record,
                    "initial",
                    user_message["content"],
                )
                if answer is None:
                    # Unsupported or invalid structured output: ordinary natural chat, explicitly deferred.
                    reply = await self._reply(model_messages)
                    assistant_message, _ = self.clarifications.persist_preparing_reply(
                        record.id, reply, awaiting_user=False
                    )
                else:
                    self._validate_decision_binding(answer, record.id, phase="initial")
                    awaiting = answer.action is ClarificationAction.ASK
                    assistant_message, _ = self.clarifications.persist_preparing_reply(
                        record.id, answer.content, awaiting_user=awaiting
                    )
            except (ModelRequestError, ModelConfigurationError, ContextBudgetExceeded):
                if record is not None:
                    self.clarifications.defer_preparing(record.id, "model_request_failed")
                raise
            except StructuredOutputError as exc:
                logger.warning(
                    "首次澄清决策不符合阶段契约 message_id=%s diagnostic=%s",
                    user_message["id"],
                    exc.code,
                )
                if record is not None:
                    try:
                        self.clarifications.defer_preparing(record.id, exc.code)
                    except Exception:
                        logger.exception("首次澄清契约错误状态无法持久化 clarification_id=%s", record.id)
            except Exception:
                logger.exception("澄清生成或持久化失败，未标记为已提问 message_id=%s", user_message["id"])
                if record is not None:
                    try:
                        self.clarifications.defer_preparing(record.id, "initial_preparation_failed")
                    except Exception:
                        logger.exception("澄清准备失败状态无法持久化 clarification_id=%s", record.id)
            else:
                self.conversations.touch(conversation_id)
                return {**assistant_message, "correction": self._correction_summary(correction_results)}

        try:
            reply = await self._reply(model_messages)
            with self.messages.database.atomic():
                assistant_message = self.messages.add(conversation_id, "assistant", reply)
                if share_reservation and not share_service.commit_chat_selection(share_reservation["token"]):
                    raise RuntimeError("分享候选保留已过期，聊天消息未保存")
        except Exception:
            if share_reservation:
                try:
                    share_service.release_chat_reservation(share_reservation["token"])
                except Exception:
                    logger.exception("释放生活分享候选保留失败 intent_id=%s", share_reservation["intent_id"])
            raise
        self.conversations.touch(conversation_id)
        return {**assistant_message, "correction": self._correction_summary(correction_results)}

    async def _continue_clarification(
        self, conversation_id: str, user_message: dict, clarification: ClarificationRecord, token: str | None
    ) -> dict:
        model_messages = self._model_messages(conversation_id, "")
        model_messages.append({"role": "system", "content": self._clarification_context_message(clarification)})
        resolution_value: str | None = None
        evidence_excerpt: str | None = None
        try:
            answer = await self._structured_generation(model_messages, clarification, "response", user_message["content"])
            if answer is None:
                reply = await self._reply(model_messages)
                assistant_message, _ = self.clarifications.persist_processing_reply(
                    clarification.id, token or "", reply, ClarificationStatus.DEFERRED, error_code="structured_output_unavailable"
                )
                return assistant_message
            try:
                self._validate_decision_binding(answer, clarification.id, phase="response")
            except StructuredOutputError as exc:
                logger.warning(
                    "澄清答复决策不符合阶段契约 clarification_id=%s diagnostic=%s",
                    clarification.id,
                    exc.code,
                )
                reply = await self._generate_nonapplication_reply(
                    model_messages, "unverified_structured_decision", user_message["content"]
                )
                assistant_message, _ = self.clarifications.persist_processing_reply(
                    clarification.id,
                    token or "",
                    reply,
                    ClarificationStatus.DEFERRED,
                    error_code=exc.code,
                )
                return assistant_message
            outcome, error_code, resolution_value, candidates = self._validated_resolution(answer, user_message["content"])
            evidence_excerpt = answer.evidence_excerpt

            if (
                outcome is ClarificationStatus.EXPLAINED
                and resolution_value in ("explicit_change", "original_was_wrong")
            ):
                application_reply = await self._apply_explained_answer(
                    model_messages, user_message, clarification, token or "", answer, candidates,
                    resolution_value, evidence_excerpt or "",
                )
                if application_reply is not None:
                    return application_reply
                outcome = ClarificationStatus.DEFERRED
                error_code = "safe_proposal_not_created"

            if resolution_value in ("denied", "temporary", "coexisting") and self.clarification_application is not None:
                if not self.clarification_application.cancel_unneeded_source(clarification.id, resolution_value):
                    outcome = ClarificationStatus.FAILED
                    error_code = "original_proposal_already_resolved"

            if answer.action is ClarificationAction.ASK and answer.resolution is ClarificationResolution.UNCLEAR:
                outcome = ClarificationStatus.CONTINUE
            elif answer.action is not ClarificationAction.ASK and outcome is ClarificationStatus.CONTINUE:
                outcome = ClarificationStatus.DEFERRED
            if outcome is ClarificationStatus.CONTINUE:
                reply = answer.content
            else:
                reply = await self._generate_nonapplication_reply(model_messages, resolution_value, user_message["content"])
            assistant_message, _ = self.clarifications.persist_processing_reply(
                clarification.id,
                token or "",
                reply,
                outcome,
                error_code=error_code,
                resolution=resolution_value,
                evidence_excerpt=evidence_excerpt,
            )
            return assistant_message
        except ContextBudgetExceeded:
            logger.info("澄清模型上下文超过预算，保留澄清状态 clarification_id=%s", clarification.id)
            try:
                self.clarifications.finish_processing(
                    clarification.id,
                    token or "",
                    ClarificationStatus.FAILED,
                    error_code="context_budget_exceeded",
                    resolution=resolution_value,
                    evidence_excerpt=evidence_excerpt,
                )
            except Exception:
                logger.exception("上下文超限后的澄清状态无法释放 clarification_id=%s", clarification.id)
            raise
        except Exception:
            logger.exception("澄清答复处理失败 clarification_id=%s", clarification.id)
            reconciled = None
            try:
                reconciled = self.clarifications.reconcile_applied(clarification.id)
            except Exception:
                logger.exception("澄清应用事实对账失败 clarification_id=%s", clarification.id)
            if reconciled is not None and reconciled.status is ClarificationStatus.APPLIED:
                # The atomic application log is authoritative; don't downgrade it because reply generation failed.
                raise
            try:
                current = self.clarifications.get(clarification.id)
                self.clarifications.finish_processing(
                    clarification.id,
                    token or "",
                    ClarificationStatus.FAILED,
                    error_code="application_interrupted" if current.application_proposal_id else "response_processing_failed",
                    resolution=resolution_value,
                    evidence_excerpt=evidence_excerpt,
                )
            except Exception:
                logger.exception("无法释放澄清处理状态 clarification_id=%s", clarification.id)
            raise

    async def _apply_explained_answer(
        self,
        model_messages: list[dict[str, str]],
        user_message: dict,
        clarification: ClarificationRecord,
        processing_token: str,
        answer: StructuredChatReply,
        candidates,
        resolution: str,
        evidence_excerpt: str,
    ) -> dict | None:
        if (
            self.correction_flow is None
            or self.clarifications is None
            or self.clarification_application is None
            or self.memory_correction_service is None
            or len(candidates) != 1
            or candidates[0].intent not in (CorrectionIntent.EXPLICIT_CORRECTION, CorrectionIntent.LONG_TERM_CHANGE)
        ):
            return None

        results = self.correction_flow.process(user_message, candidates)
        if len(results) != 1 or results[0].status not in (
            CorrectionFlowStatus.PROPOSAL_CREATED,
            CorrectionFlowStatus.PROPOSAL_REUSED,
        ) or results[0].proposal is None:
            return None
        proposal = results[0].proposal
        if proposal.target_memory_id is None:
            return None

        try:
            self.clarifications.link_application_proposal(
                clarification.id,
                processing_token,
                proposal.proposal_id,
                resolution=resolution,
                evidence_excerpt=evidence_excerpt,
            )
        except Exception:
            self._cancel_unlinked_proposal(clarification.id, proposal.proposal_id)
            raise

        try:
            result = self.clarification_application.apply_linked(clarification.id, processing_token)
        except (ProposalStateError, ProposalApplicationError, IdempotencyConflictError, DuplicateMemoryError, MemoryNotFoundError, MessageNotFoundError, ProposalNotFoundError):
            logger.exception("澄清 Proposal 应用发生业务冲突 clarification_id=%s", clarification.id)
            reply = await self._generate_nonapplication_reply(
                model_messages, "application_failed", user_message["content"]
            )
            assistant_message, _ = self.clarifications.persist_processing_reply(
                clarification.id,
                processing_token,
                reply,
                ClarificationStatus.FAILED,
                error_code="application_conflict",
                resolution=resolution,
                evidence_excerpt=evidence_excerpt,
            )
            return assistant_message

        final_messages = list(model_messages)
        replacement = result.replacement_memory
        applied_content = replacement.content if replacement is not None else ""
        final_messages.append(
            {
                "role": "system",
                "content": (
                    "系统已确认本次用户答复对应的长期记忆更新已经成功提交。"
                    f"当前正式记忆内容为：{applied_content}。请结合大肥鱼人格和上下文自然回应，"
                    "可以表达已经记住，但不要提及 Proposal、事务、状态码或数据库。"
                ),
            }
        )
        reply = await self._reply(final_messages)
        assistant_message, _ = self.clarifications.persist_processing_reply(
            clarification.id,
            processing_token,
            reply,
            ClarificationStatus.APPLIED,
            resolution=resolution,
            evidence_excerpt=evidence_excerpt,
        )
        return assistant_message

    def _cancel_unlinked_proposal(self, clarification_id: str, proposal_id: str) -> None:
        if self.proposal_service is None:
            return
        try:
            proposal = self.proposal_service.get_proposal(proposal_id)
            if proposal.status is ProposalStatus.PENDING:
                self.proposal_service.cancel_proposal(
                    proposal_id,
                    reason="澄清目标或来源校验未通过，未执行应用",
                    idempotency_key=f"clarification:{clarification_id}:cancel-unlinked:{proposal_id}",
                )
        except Exception:
            logger.exception("未关联 Proposal 无法安全取消 clarification_id=%s", clarification_id)

    async def _generate_nonapplication_reply(
        self, messages: list[dict[str, str]], resolution: str | None, user_text: str
    ) -> str:
        prompt = list(messages)
        prompt.append(
            {
                "role": "system",
                "content": (
                    f"用户刚才的真实表达属于 {resolution or '未能验证'}。正式记忆没有因本轮答复而更新。"
                    f"用户原话：{user_text!r}。请自然回应当前话题并尊重用户的说明；"
                    "不得声称已经永久记住、更新或应用这条信息。不要输出技术状态。"
                ),
            }
        )
        return await self._reply(prompt)

    async def _structured_generation(
        self, messages: list[dict[str, str]], clarification: ClarificationRecord, phase: str, user_text: str
    ) -> StructuredChatReply | None:
        method = getattr(self.model, "reply_structured", None)
        if not callable(method):
            return None
        prompt = list(messages)
        prompt.append({
            "role": "system",
            "content": "<memory_clarification_instruction>\n"
            + self._decision_instruction(clarification, phase, user_text)
            + "\n</memory_clarification_instruction>",
        })
        try:
            result = await self._structured_reply(method, prompt)
            if not isinstance(result, StructuredChatReply):
                raise StructuredOutputError("结构化模型响应类型无效", code="invalid_result_type")
            if result.ignored_extra_fields:
                logger.info("结构化澄清输出含可忽略的额外字段 clarification_id=%s", clarification.id)
            return result
        except StructuredOutputError as exc:
            logger.warning(
                "结构化澄清输出不可用，回退普通聊天 clarification_id=%s diagnostic=%s",
                clarification.id,
                exc.code,
            )
            return None
        except (ContextBudgetExceeded, ModelRequestError, ModelConfigurationError):
            raise
        except Exception as exc:
            logger.warning(
                "结构化澄清处理不可用，回退普通聊天 clarification_id=%s error_type=%s",
                clarification.id,
                type(exc).__name__,
            )
            return None

    async def _reply(self, messages: list[dict[str, str]]) -> str:
        with measure("context_budget"):
            result = self.context_window.fit(messages)
        self.last_context_report = result
        count("estimated_input_tokens", result.estimated_after)
        count("dropped_turns", result.dropped_turns)
        count("plain_model_calls")
        checkpoint("model_generation")
        try:
            return await self.model.reply(result.messages)
        finally:
            checkpoint("reply_persistence")

    async def _structured_reply(self, method, messages: list[dict[str, str]]) -> StructuredChatReply:
        with measure("context_budget"):
            result = self.context_window.fit(messages)
        self.last_context_report = result
        count("estimated_input_tokens", result.estimated_after)
        count("dropped_turns", result.dropped_turns)
        count("structured_model_calls")
        checkpoint("model_generation")
        try:
            return await method(result.messages)
        finally:
            checkpoint("reply_persistence")

    def _model_messages(self, conversation_id: str, memory_context: str) -> list[dict[str, str]]:
        history = self.messages.list_for_conversation(conversation_id)
        messages = [{"role": "system", "content": self.system_prompt}]
        if memory_context:
            messages.append({"role": "system", "content": memory_context})
        messages.extend({"role": item["role"], "content": item["content"]} for item in history)
        return messages

    def _runtime_capabilities(self, query: str, character_id: str) -> dict:
        """Include only capabilities relevant to the current question."""
        result: dict = {}
        if any(cue in query for cue in (
            "主动", "发消息", "发信息", "联系我", "来找我", "找我", "通知我",
            "给我留言", "来敲我",
        )):
            if self.contact_service is None:
                contact = {"supported": False, "enabled": False, "policy_allows_now": False,
                           "ready_in_app_messages": 0, "channel": "in_app_only",
                           "browser_display_and_confirmation_required": True, "external_push": False}
            else:
                contact = self.contact_service.delivery_context(character_id)
            contact["background_checks_enabled"] = bool(contact["enabled"] and self.runtime_features.get("contact_background_enabled"))
            contact["model_generated_contact_enabled"] = bool(contact["enabled"] and self.runtime_features.get("contact_model_generation_enabled"))
            result["proactive_contact"] = contact
        if any(cue in query for cue in ("在哪里", "在哪", "住哪里", "住在", "房间", "数字空间", "身体", "看得见", "听得见")):
            result["digital_space"] = {
                "web_chat_surface": True,
                "virtual_life_records": True,
                "immersive_room": False,
                "physical_location": False,
                "physical_senses": False,
            }
        return result

    @staticmethod
    def _single_clarification_context(results: tuple[CorrectionFlowResult, ...]) -> dict | None:
        eligible = []
        for result in results:
            if result.clarification_context is None:
                continue
            if result.status in (
                CorrectionFlowStatus.CLARIFICATION_NEEDED,
                CorrectionFlowStatus.PROPOSAL_CREATED,
                CorrectionFlowStatus.PROPOSAL_REUSED,
            ):
                eligible.append(result)
        if len(eligible) != 1:
            return None
        result = eligible[0]
        if len(result.clarification_context.get("matched_memories", ())) > 5:
            return None
        source_proposal_id = None
        if result.status in (CorrectionFlowStatus.PROPOSAL_CREATED, CorrectionFlowStatus.PROPOSAL_REUSED):
            if result.proposal is None or result.proposal.target_memory_id is None:
                return None
            source_proposal_id = result.proposal.proposal_id
        return {"context": result.clarification_context, "source_proposal_id": source_proposal_id}

    @staticmethod
    def _clarification_context_message(record: ClarificationRecord) -> str:
        context = record.conflict_context
        matches = "\n".join(f"- {item['content']}" for item in context["matched_memories"])
        return (
            "<memory_clarification_context>\n"
            "以下是需要理解的历史记忆和用户当前表达。历史记忆只是曾记录的内容，可能已经过时；"
            "不得把它当作当前事实，也不得压过用户最新表达。\n"
            f"历史记忆候选：\n{matches}\n"
            f"用户当时表达：{context['candidate']['content']}\n"
            f"关联依据：{'；'.join(context['evidence'])}\n"
            f"需要理解的差异：{context['reason']}\n"
            "请结合人格和完整对话自然回应。不要提及技术状态、Proposal 或置信度。"
            "不必强行追问；如果当前话题或情绪更重要，可以先回应当前话题。\n"
            "</memory_clarification_context>"
        )

    @staticmethod
    def _decision_instruction(record: ClarificationRecord, phase: str, user_text: str) -> str:
        def example(reply: str, action: str, resolution: str, excerpt: str | None) -> str:
            return json.dumps(
                {
                    "reply": reply,
                    "clarification": {
                        "clarification_id": record.id,
                        "action": action,
                        "resolution": resolution,
                        "evidence_excerpt": excerpt,
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )

        common = (
            "只输出一个 JSON 对象，不得附加 Markdown 代码围栏或对象外说明。"
            "reply 是给用户看的自然中文；不得在 reply 中提及内部协议、Proposal 或状态。"
            f"clarification_id 必须原样为 '{record.id}'。action 只能是 ask、defer、normal。"
        )
        if phase == "initial":
            return (
                "当前阶段是 initial：系统刚从用户消息发现可能与旧记忆不同的信息，尚未向用户提出并保存本次澄清问题。"
                "用户当前这句话是触发澄清的原始表达，不是对本次澄清问题的回答；即使用户说了‘已经改变’或‘永久改用’，"
                "也不得在本阶段把它分类为 explicit_change 或其他答复结论。"
                "你只决定这轮是否适合自然追问：确实询问这项变化时 action=ask；暂缓时 action=defer；"
                "优先回应其他话题或不发起澄清时 action=normal。不要强迫每次变化都追问。"
                "本阶段 resolution 必须为 none，evidence_excerpt 必须为 null。\n"
                f"ask 示例：{example('咦，你这是已经把主要开发环境换掉了吗？', 'ask', 'none', None)}\n"
                f"defer 示例：{example('哦，听起来你最近在折腾新的开发环境。', 'defer', 'none', None)}\n"
                f"normal 示例：{example('Linux 的终端确实挺有意思，你最近在忙什么项目？', 'normal', 'none', None)}\n"
                f"{common}\n当前触发澄清的用户原文：{user_text!r}"
            )
        if phase == "response":
            return (
                "当前阶段是 response：这条用户消息已经关联到一条先前实际保存的澄清问题。"
                "只依据这条最新 user 消息分类，不得把大肥鱼自己的猜测、旧消息或自己的提问当作用户证据。"
                "resolution 必须且只能是 explicit_change、original_was_wrong、coexisting、denied、temporary、unclear、off_topic 之一；"
                "none 在本阶段无效。action 表示这轮回复是否继续追问、暂缓或正常回应；不要把 action 当作用户事实分类。"
                "resolution 不是 none 时，evidence_excerpt 必须逐字取自当前用户答复；证据不足时选 unclear，不能编造摘录。"
                "明确长期变化或原记忆错误必须由用户直接、清楚地表达；‘嗯’、‘好吧’等简短附和不构成证据。\n"
                f"明确答复示例（仅当当前用户原话确实包含该句时才可照此摘录）：{example('哦，这下我听明白啦。', 'normal', 'explicit_change', '我已经永久改用 Linux 作为主要开发环境')}\n"
                f"含糊答复示例：{example('那我再陪你慢慢想想。', 'ask', 'unclear', '我还没想好，可能吧')}\n"
                f"{common}\n当前需要分类的用户答复原文：{user_text!r}"
            )
        raise StructuredOutputError("未知的澄清决策阶段", code="unknown_decision_phase")

    @staticmethod
    def _validate_decision_binding(
        answer: StructuredChatReply, clarification_id: str, *, phase: str
    ) -> None:
        if answer.clarification_id != clarification_id:
            raise StructuredOutputError(
                "结构化决策引用了错误的澄清记录", code="clarification_id_mismatch"
            )
        if phase == "initial" and answer.resolution is not ClarificationResolution.NONE:
            raise StructuredOutputError(
                "首次发现阶段不能分类为用户答复", code="initial_resolution_must_be_none"
            )
        if phase == "initial" and answer.evidence_excerpt is not None:
            raise StructuredOutputError(
                "首次发现阶段不能提交用户答复证据", code="initial_evidence_must_be_null"
            )
        if phase == "response" and answer.resolution is ClarificationResolution.NONE:
            raise StructuredOutputError(
                "澄清答复阶段必须提供明确分类", code="response_resolution_required"
            )
        if phase not in {"initial", "response"}:
            raise StructuredOutputError("未知的澄清决策阶段", code="unknown_decision_phase")

    def _validated_resolution(
        self, answer: StructuredChatReply, user_text: str
    ) -> tuple[ClarificationStatus, str | None, str, list]:
        resolution = answer.resolution
        excerpt = answer.evidence_excerpt
        normalized_text = " ".join(user_text.split())
        normalized_excerpt = " ".join((excerpt or "").split())
        if resolution is ClarificationResolution.NONE:
            return ClarificationStatus.DEFERRED, "resolution_not_provided", "unclear", []
        if not normalized_excerpt or normalized_excerpt not in normalized_text:
            return ClarificationStatus.DEFERRED, "evidence_not_from_user_message", "unclear", []
        normalized = re.sub(r"[\s，。！？、,.!?；;：:]", "", normalized_text)
        candidates = self.memory_extractor.extract_text(user_text) if self.memory_extractor else []
        intents = {candidate.intent for candidate in candidates}
        if resolution in (ClarificationResolution.EXPLICIT_CHANGE, ClarificationResolution.ORIGINAL_WAS_WRONG):
            acknowledgement_only = {
                "嗯", "嗯嗯", "哦", "好", "好的", "好吧", "随便", "是", "对", "行", "可以", "没事"
            }
            if normalized in acknowledgement_only:
                return ClarificationStatus.DEFERRED, "ambiguous_acknowledgement", "unclear", candidates
            if not self._is_direct_user_statement(user_text) or any(
                marker in user_text for marker in ("可能", "也许", "假如", "如果", "要是", "会不会", "？", "?")
            ):
                return ClarificationStatus.DEFERRED, "statement_is_not_direct_or_certain", "unclear", candidates
            durable_markers = ("永久", "长期", "从现在起", "以后都", "已经改成", "已经换成", "不再", "决定改为")
            if resolution is ClarificationResolution.ORIGINAL_WAS_WRONG:
                if CorrectionIntent.EXPLICIT_CORRECTION not in intents:
                    return ClarificationStatus.DEFERRED, "original_error_not_confirmed", "unclear", candidates
                return ClarificationStatus.EXPLAINED, None, "original_was_wrong", candidates
            if CorrectionIntent.LONG_TERM_CHANGE not in intents or not any(marker in user_text for marker in durable_markers):
                return ClarificationStatus.DEFERRED, "no_explicit_durable_change_signal", "unclear", candidates
            return ClarificationStatus.EXPLAINED, None, "explicit_change", candidates
        if resolution is ClarificationResolution.COEXISTING:
            if not self._is_coexisting_user_statement(user_text):
                return ClarificationStatus.DEFERRED, "coexistence_not_confirmed", "unclear", candidates
            return ClarificationStatus.DEFERRED, None, "coexisting", candidates
        if resolution is ClarificationResolution.DENIED:
            if not self._is_direct_user_statement(user_text) or not any(
                marker in user_text
                for marker in ("没改", "没有改", "没换", "没有换", "没有变化", "并没有", "并未改变", "仍然是", "仍然用")
            ):
                return ClarificationStatus.DEFERRED, "denial_not_supported_by_user_text", "unclear", candidates
            return ClarificationStatus.DENIED, None, "denied", candidates
        if resolution is ClarificationResolution.TEMPORARY:
            if not self._is_direct_user_statement(user_text) or not any(
                candidate.intent is CorrectionIntent.TEMPORARY_EXPERIMENT for candidate in candidates
            ):
                return ClarificationStatus.DEFERRED, "temporary_signal_not_supported", "unclear", candidates
            return ClarificationStatus.TEMPORARY, None, "temporary", candidates
        if resolution is ClarificationResolution.UNCLEAR:
            return ClarificationStatus.CONTINUE, "user_response_unclear", "unclear", candidates
        if resolution is ClarificationResolution.OFF_TOPIC:
            return ClarificationStatus.DEFERRED, "user_changed_topic", "off_topic", candidates
        return ClarificationStatus.DEFERRED, "unrecognized_resolution", "unclear", candidates

    @staticmethod
    def _is_direct_user_statement(text: str) -> bool:
        value = " ".join(text.split())
        for prefix in ("其实，", "其实", "对，", "对", "是的，", "是的", "没错，", "没错", "不是，", "不是", "嗯，"):
            if value.startswith(prefix):
                value = value[len(prefix):].lstrip(" ，,：:")
                break
        third_party = ("我朋友", "我同事", "我家人", "我同学", "有人", "别人", "他说", "她说", "他们说")
        return value.startswith("我") and not value.startswith(third_party)

    @staticmethod
    def _is_coexisting_user_statement(text: str) -> bool:
        value = " ".join(text.split())
        third_party = ("我朋友", "我同事", "我家人", "我同学", "有人", "别人", "他说", "她说", "他们说")
        explicit_coexistence = (
            "同时" in value
            or "兼用" in value
            or re.search(r"(?:两个|两种|两边).{0,24}都|既.{1,24}也", value) is not None
        )
        return "我" in value and not value.startswith(third_party) and explicit_coexistence

    @staticmethod
    def _correction_summary(results: tuple[CorrectionFlowResult, ...]) -> dict:
        if not results:
            return {"status": CorrectionFlowStatus.NO_ACTION.value, "items": []}
        items = []
        for result in results:
            item = {"status": result.status.value}
            if result.proposal is not None:
                item.update(
                    {
                        "proposal_id": result.proposal.proposal_id,
                        "target_memory_id": result.proposal.target_memory_id,
                        "relation": result.proposal.relation.value,
                        "confidence": result.proposal.confidence,
                        "evidence": list(result.proposal.evidence),
                    }
                )
            if result.error_code is not None:
                item["error_code"] = result.error_code
            if result.clarification:
                item["clarification"] = list(result.clarification)
            items.append(item)
        statuses = {item["status"] for item in items}
        status = statuses.pop() if len(statuses) == 1 else "mixed"
        return {"status": status, "items": items}
