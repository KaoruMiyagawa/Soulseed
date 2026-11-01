from __future__ import annotations

import logging
import re

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
    ):
        self.conversations, self.messages, self.model, self.system_prompt = conversations, messages, model, system_prompt
        self.memory_service, self.memory_extractor = memory_service, memory_extractor
        self.memory_retriever, self.memory_context_builder = memory_retriever, memory_context_builder
        self.correction_flow = correction_flow
        self.clarifications = clarification_repository
        self.proposal_service = proposal_service
        self.clarification_application = clarification_application
        self.memory_correction_service = memory_correction_service

    async def send(self, conversation_id: str, content: str) -> dict:
        self.conversations.get(conversation_id)
        user_message = self.messages.add(conversation_id, "user", content)

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

        memory_context = ""
        if self.memory_retriever and self.memory_context_builder:
            try:
                memory_context = self.memory_context_builder.build(self.memory_retriever.retrieve(content))
            except Exception:
                logger.exception("记忆检索或上下文组装失败，继续执行聊天请求 message_id=%s", user_message["id"])

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
                    reply = await self.model.reply(model_messages)
                    assistant_message, _ = self.clarifications.persist_preparing_reply(
                        record.id, reply, awaiting_user=False
                    )
                else:
                    self._validate_decision_binding(answer, record.id, initial=True)
                    awaiting = answer.action is ClarificationAction.ASK
                    assistant_message, _ = self.clarifications.persist_preparing_reply(
                        record.id, answer.content, awaiting_user=awaiting
                    )
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

        reply = await self.model.reply(model_messages)
        assistant_message = self.messages.add(conversation_id, "assistant", reply)
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
                reply = await self.model.reply(model_messages)
                assistant_message, _ = self.clarifications.persist_processing_reply(
                    clarification.id, token or "", reply, ClarificationStatus.DEFERRED, error_code="structured_output_unavailable"
                )
                return assistant_message
            self._validate_decision_binding(answer, clarification.id)
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
        reply = await self.model.reply(final_messages)
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
        return await self.model.reply(prompt)

    async def _structured_generation(
        self, messages: list[dict[str, str]], clarification: ClarificationRecord, phase: str, user_text: str
    ) -> StructuredChatReply | None:
        method = getattr(self.model, "reply_structured", None)
        if not callable(method):
            return None
        prompt = list(messages)
        prompt.append({"role": "system", "content": self._decision_instruction(clarification, phase, user_text)})
        try:
            result = await method(prompt)
            if not isinstance(result, StructuredChatReply):
                raise StructuredOutputError("结构化模型响应类型无效")
            return result
        except Exception:
            logger.warning("结构化澄清输出不可用，回退普通聊天 clarification_id=%s", clarification.id, exc_info=True)
            return None

    def _model_messages(self, conversation_id: str, memory_context: str) -> list[dict[str, str]]:
        history = self.messages.list_for_conversation(conversation_id)
        messages = [{"role": "system", "content": self.system_prompt}]
        if memory_context:
            messages.append({"role": "system", "content": memory_context})
        messages.extend({"role": item["role"], "content": item["content"]} for item in history)
        return messages

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
        mode = (
            "判断这轮回复是否适合自然地询问用户刚表达的变化；也可以暂缓或正常回应。"
            if phase == "initial"
            else "判断用户最新回复是在明确解释长期变化、否认变化、表达临时尝试、仍不明确，还是转移话题。可以继续自然询问，也可以暂缓或正常回应。"
        )
        return (
            "内部输出协议：只生成一个 JSON 对象，不得将协议内容放入 reply。reply 是直接给用户看的自然中文。\n"
            f"clarification_id 必须原样为 {record.id!r}。{mode}\n"
            "clarification.action 只能是 ask、defer、normal；resolution 只能是 none、explicit_change、original_was_wrong、coexisting、denied、temporary、unclear、off_topic。"
            "只有确实提出与这条澄清相关的问题才选 ask；不能按问号或措辞推断是否询问。"
            "resolution 若不是 none，evidence_excerpt 必须逐字摘录当前用户消息中的依据；否则填 null。"
            "初次阶段 resolution 应为 none。明确解释必须是用户自己的持续性事实表达；简短附和不能算明确解释。"
            f"当前需要验证来源的用户原文：{user_text!r}"
        )

    @staticmethod
    def _validate_decision_binding(
        answer: StructuredChatReply, clarification_id: str, *, initial: bool = False
    ) -> None:
        if answer.clarification_id != clarification_id:
            raise StructuredOutputError("结构化决策引用了错误的澄清记录")
        if initial and answer.resolution is not ClarificationResolution.NONE:
            raise StructuredOutputError("初次澄清不能将模型判断伪装成用户答复")

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
