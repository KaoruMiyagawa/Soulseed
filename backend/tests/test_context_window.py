from __future__ import annotations

import pytest

from app.context_window import (
    ConservativeTokenEstimator,
    ContextBudgetExceeded,
    ContextWindowLimits,
    ModelContextWindow,
)
from app.chat_service import ChatService
from app.db import Database
from app.ownership import character_scope
from app.repositories import ConversationRepository, MessageRepository


class CharacterEstimator:
    """Deterministic test estimator: one unit per content character."""

    def count_messages(self, messages):
        return sum(len(message["content"]) for message in messages)


def limits(**overrides):
    values = dict(
        max_input_tokens=10_000,
        max_persona_tokens=2_000,
        max_expression_tokens=2_000,
        max_clarification_tokens=2_000,
        max_system_control_tokens=2_000,
        max_current_message_tokens=2_000,
        max_history_tokens=1_000,
    )
    values.update(overrides)
    return ContextWindowLimits(**values)


def test_120_turn_history_trims_oldest_complete_turns_and_keeps_current():
    messages = [{"role": "system", "content": "persona"}]
    for index in range(120):
        messages.extend([
            {"role": "user", "content": f"u{index:03d}"},
            {"role": "assistant", "content": f"a{index:03d}"},
        ])
    messages.append({"role": "user", "content": "CURRENT"})
    window = ModelContextWindow(
        limits(max_input_tokens=100, max_history_tokens=16, max_current_message_tokens=20),
        estimator=CharacterEstimator(),
    )

    result = window.fit(messages)
    roles_and_content = [(item["role"], item["content"]) for item in result.messages]

    assert roles_and_content[0] == ("system", "persona")
    assert roles_and_content[-1] == ("user", "CURRENT")
    assert result.dropped_turns == 118
    assert [text for role, text in roles_and_content if role == "user"] == ["u118", "u119", "CURRENT"]
    for user, assistant in zip(
        [m for m in roles_and_content if m[0] == "user" and m[1] != "CURRENT"],
        [m for m in roles_and_content if m[0] == "assistant"],
    ):
        assert int(assistant[1][1:]) == int(user[1][1:])
    assert result.estimated_before > result.estimated_after


def test_current_message_over_budget_is_rejected_without_truncating_it():
    window = ModelContextWindow(
        limits(max_input_tokens=100, max_current_message_tokens=4, max_history_tokens=20),
        estimator=CharacterEstimator(),
    )
    with pytest.raises(ContextBudgetExceeded, match="当前消息") as error:
        window.fit([
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "0123456789"},
        ])
    assert error.value.estimated_tokens == 10


def test_legacy_oversized_unanswered_turn_is_dropped_whole_without_blocking_next_turn():
    messages = [
        {"role": "system", "content": "p"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "legacy oversize " * 50},
        {"role": "user", "content": "new current turn"},
    ]
    result = ModelContextWindow(
        limits(max_input_tokens=100, max_history_tokens=10, max_current_message_tokens=30),
        estimator=CharacterEstimator(),
    ).fit(messages)

    assert result.messages == [messages[0], messages[-1]]
    assert result.dropped_turns == 2


@pytest.mark.asyncio
async def test_chat_rejects_oversized_current_message_before_persistence(tmp_path):
    database = Database(str(tmp_path / "oversize.db"))
    database.initialize()
    conversations, messages = ConversationRepository(database), MessageRepository(database)
    conversation = conversations.create()

    class Model:
        calls = 0

        async def reply(self, _request):
            self.calls += 1
            return "unexpected"

    model = Model()
    chat = ChatService(
        conversations,
        messages,
        model,
        "p",
        context_window=ModelContextWindow(
            limits(max_input_tokens=100, max_current_message_tokens=4),
            estimator=CharacterEstimator(),
        ),
    )
    with pytest.raises(ContextBudgetExceeded):
        await chat.send(conversation["id"], "0123456789")
    assert model.calls == 0
    assert messages.list_for_conversation(conversation["id"]) == []


def test_expression_and_clarification_system_context_are_pinned():
    messages = [
        {"role": "system", "content": "persona"},
        {"role": "system", "content": '<expression_context>{"fact":"completed music"}</expression_context>'},
        {"role": "system", "content": '<memory_clarification_context>old memory and source</memory_clarification_context>'},
        {"role": "system", "content": '<memory_clarification_instruction>classify this turn</memory_clarification_instruction>'},
        {"role": "user", "content": "我已经改变了"},
    ]
    result = ModelContextWindow(limits(), estimator=CharacterEstimator()).fit(messages)
    assert result.messages == messages
    assert result.bucket_tokens["persona"] == len("persona")
    assert result.bucket_tokens["expression"] == len(messages[1]["content"])
    assert result.bucket_tokens["clarification"] == len(messages[2]["content"] + messages[3]["content"])
    assert result.bucket_tokens["current_message"] == len(messages[-1]["content"])


def test_oversize_required_clarification_is_rejected_instead_of_trimmed():
    messages = [
        {"role": "system", "content": "persona"},
        {"role": "system", "content": '<memory_clarification_context>' + "关键来源" * 20 + '</memory_clarification_context>'},
        {"role": "user", "content": "是的"},
    ]
    with pytest.raises(ContextBudgetExceeded) as error:
        ModelContextWindow(
            limits(max_clarification_tokens=40), estimator=CharacterEstimator()
        ).fit(messages)
    assert error.value.bucket == "clarification"


@pytest.mark.parametrize("budget, expected_users", [(8, ["u8", "u9", "now"]), (4, ["u9", "now"])])
def test_history_selection_changes_predictably_with_budget(budget, expected_users):
    messages = [{"role": "system", "content": "p"}]
    for index in range(10):
        messages.extend([
            {"role": "user", "content": f"u{index}"},
            {"role": "assistant", "content": f"a{index}"},
        ])
    messages.append({"role": "user", "content": "now"})
    result = ModelContextWindow(
        limits(max_input_tokens=100, max_history_tokens=budget, max_current_message_tokens=10),
        estimator=CharacterEstimator(),
    ).fit(messages)
    assert [m["content"] for m in result.messages if m["role"] == "user"] == expected_users


def test_estimator_is_explicitly_an_estimate_and_handles_cjk_conservatively():
    estimator = ConservativeTokenEstimator()
    english = estimator.count_messages([{"role": "user", "content": "hello world"}])
    chinese = estimator.count_messages([{"role": "user", "content": "今天吃了吗"}])
    assert english > 0 and chinese > english


@pytest.mark.asyncio
async def test_chat_sends_bounded_history_but_keeps_complete_persistent_history(tmp_path):
    path = tmp_path / "history.db"
    database = Database(str(path))
    database.initialize()
    conversations, messages = ConversationRepository(database), MessageRepository(database)
    conversation = conversations.create()
    for index in range(110):
        messages.add(conversation["id"], "user", f"u{index:03d}")
        messages.add(conversation["id"], "assistant", f"a{index:03d}")
    captured = []

    class Model:
        async def reply(self, request):
            captured.extend(request)
            return "回复"

    chat = ChatService(
        conversations,
        messages,
        Model(),
        "fish",
        context_window=ModelContextWindow(
            limits(max_input_tokens=100, max_history_tokens=16, max_current_message_tokens=20),
            estimator=CharacterEstimator(),
        ),
    )
    await chat.send(conversation["id"], "current")

    assert [(m["role"], m["content"]) for m in captured] == [
        ("system", "fish"),
        ("user", "u108"), ("assistant", "a108"),
        ("user", "u109"), ("assistant", "a109"),
        ("user", "current"),
    ]
    assert len(messages.list_for_conversation(conversation["id"])) == 222
    restarted = Database(str(path))
    restarted.initialize()
    assert len(MessageRepository(restarted).list_for_conversation(conversation["id"])) == 222


@pytest.mark.asyncio
async def test_chat_context_never_contains_another_characters_history(tmp_path):
    database = Database(str(tmp_path / "characters.db"))
    database.initialize()
    with database.connection() as connection:
        connection.execute("INSERT INTO users VALUES ('owner-b', datetime('now'))")
        connection.execute("INSERT INTO characters VALUES ('fish-b', 'owner-b', 'owned', datetime('now'))")
    conversations, messages = ConversationRepository(database), MessageRepository(database)
    with character_scope("big_fish"):
        fish = conversations.create("fish conversation")
        messages.add(fish["id"], "user", "大肥鱼的过去")
        fish_id = fish["id"]
    with character_scope("fish-b"):
        other = conversations.create("other conversation")
        messages.add(other["id"], "user", "other character secret")
    captured = []

    class Model:
        async def reply(self, request):
            captured.extend(request)
            return "继续聊"

    with character_scope("big_fish"):
        await ChatService(conversations, messages, Model(), "fish").send(fish_id, "当前问题")
    assert "other character secret" not in "\n".join(item["content"] for item in captured)
    with character_scope("fish-b"):
        assert [m["content"] for m in messages.list_for_conversation(other["id"])] == [
            "other character secret"
        ]
