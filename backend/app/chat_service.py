import logging

from .memory.extractor import DeterministicMemoryExtractor
from .memory.context_builder import MemoryContextBuilder
from .memory.retriever import MemoryRetriever
from .memory.service import MemoryService
from .model_adapter import ModelAdapter
from .repositories import ConversationRepository, MessageRepository

logger = logging.getLogger(__name__)


class ChatService:
    def __init__(self, conversations: ConversationRepository, messages: MessageRepository, model: ModelAdapter, system_prompt: str, memory_service: MemoryService | None = None, memory_extractor: DeterministicMemoryExtractor | None = None, memory_retriever: MemoryRetriever | None = None, memory_context_builder: MemoryContextBuilder | None = None):
        self.conversations, self.messages, self.model, self.system_prompt = conversations, messages, model, system_prompt
        self.memory_service, self.memory_extractor = memory_service, memory_extractor
        self.memory_retriever, self.memory_context_builder = memory_retriever, memory_context_builder

    async def send(self, conversation_id: str, content: str) -> dict:
        self.conversations.get(conversation_id)
        user_message = self.messages.add(conversation_id, "user", content)
        memory_context = ""
        if self.memory_retriever and self.memory_context_builder:
            try:
                memory_context = self.memory_context_builder.build(self.memory_retriever.retrieve(content))
            except Exception:
                logger.exception("记忆检索或上下文组装失败，继续执行聊天请求 message_id=%s", user_message["id"])
        if self.memory_service and self.memory_extractor:
            try:
                for candidate in self.memory_extractor.extract(user_message):
                    self.memory_service.persist_candidate(candidate, user_message)
            except Exception:
                logger.exception("记忆提取失败，继续执行聊天请求 message_id=%s", user_message["id"])
        history = self.messages.list_for_conversation(conversation_id)
        model_messages = [{"role": "system", "content": self.system_prompt}]
        if memory_context:
            model_messages.append({"role": "system", "content": memory_context})
        model_messages.extend({"role": m["role"], "content": m["content"]} for m in history)
        reply = await self.model.reply(model_messages)
        assistant_message = self.messages.add(conversation_id, "assistant", reply)
        self.conversations.touch(conversation_id)
        return assistant_message

