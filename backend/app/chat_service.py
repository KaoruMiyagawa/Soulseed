import logging

from .memory.context_builder import MemoryContextBuilder
from .memory.correction_flow_service import CorrectionFlowResult, CorrectionFlowService, CorrectionFlowStatus
from .memory.extractor import DeterministicMemoryExtractor
from .memory.models import CorrectionIntent
from .memory.retriever import MemoryRetriever
from .memory.service import MemoryService
from .model_adapter import ModelAdapter
from .repositories import ConversationRepository, MessageRepository

logger = logging.getLogger(__name__)


class ChatService:
    def __init__(self, conversations: ConversationRepository, messages: MessageRepository, model: ModelAdapter, system_prompt: str, memory_service: MemoryService | None = None, memory_extractor: DeterministicMemoryExtractor | None = None, memory_retriever: MemoryRetriever | None = None, memory_context_builder: MemoryContextBuilder | None = None, correction_flow: CorrectionFlowService | None = None):
        self.conversations, self.messages, self.model, self.system_prompt = conversations, messages, model, system_prompt
        self.memory_service, self.memory_extractor = memory_service, memory_extractor
        self.memory_retriever, self.memory_context_builder = memory_retriever, memory_context_builder
        self.correction_flow = correction_flow

    async def send(self, conversation_id: str, content: str) -> dict:
        self.conversations.get(conversation_id)
        user_message = self.messages.add(conversation_id, "user", content)
        memory_context = ""
        if self.memory_retriever and self.memory_context_builder:
            try:
                memory_context = self.memory_context_builder.build(self.memory_retriever.retrieve(content))
            except Exception:
                logger.exception("记忆检索或上下文组装失败，继续执行聊天请求 message_id=%s", user_message["id"])
        correction_results: tuple[CorrectionFlowResult, ...] = ()
        if self.memory_service and self.memory_extractor:
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

                correction_candidates = tuple(
                    candidate for candidate in candidates if candidate.intent is not CorrectionIntent.NONE
                )
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
        history = self.messages.list_for_conversation(conversation_id)
        model_messages = [{"role": "system", "content": self.system_prompt}]
        if memory_context:
            model_messages.append({"role": "system", "content": memory_context})
        model_messages.extend({"role": m["role"], "content": m["content"]} for m in history)
        reply = await self.model.reply(model_messages)
        assistant_message = self.messages.add(conversation_id, "assistant", reply)
        self.conversations.touch(conversation_id)
        return {**assistant_message, "correction": self._correction_summary(correction_results)}

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

