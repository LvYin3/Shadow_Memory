from __future__ import annotations

from types import SimpleNamespace

from astrbot_plugin_shadow_memory.interaction.decision_context import (
    build_conversation,
    context_for_gate,
    context_with_response,
)


class _Request:
    def __init__(self, contexts, *, system="SYS", prompt="CURRENT"):
        self.contexts = contexts
        self.system_prompt = system
        self.prompt = prompt
        self.extra_user_content_parts = [SimpleNamespace(text="FIXED")]


def test_builds_text_equivalent_context():
    request = _Request(
        [
            {"role": "user", "content": "旧问题"},
            {
                "role": "assistant",
                "content": "旧回答",
                "tool_calls": [
                    {
                        "function": {
                            "name": "say",
                            "arguments": '{"text": "你好"}',
                        }
                    }
                ],
            },
            {"role": "tool", "name": "say", "content": "已说"},
            {"role": "user", "content": [{"type": "text", "text": "新问题"}]},
        ]
    )
    context = build_conversation(request, max_history_chars=1000)
    assert context["system_prompt"] == "SYS"
    assert context["prompt"] == "CURRENT"
    assert context["extra_text"] == ["FIXED"]
    assert context["messages"][1]["tool_calls"][0]["arguments"] == {"text": "你好"}
    assert context["messages"][-1]["content"] == "新问题"


def test_truncation_keeps_newest_turn_and_full_system():
    request = _Request(
        [
            {"role": "user", "content": "old" * 100},
            {"role": "assistant", "content": "old-reply" * 100},
            {"role": "user", "content": "new-question"},
            {"role": "assistant", "content": "new-reply"},
        ],
        system="SYSTEM",
    )
    context = build_conversation(request, max_history_chars=40)
    assert context["system_prompt"] == "SYSTEM"
    assert context["messages"][-1]["content"] == "new-reply"
    assert context["truncated"] is True


def test_latest_turn_is_truncated_when_alone_over_budget():
    request = _Request([{"role": "user", "content": "x" * 200}])
    context = build_conversation(request, max_history_chars=30)
    assert len(context["messages"]) == 1
    assert len(context["messages"][0]["content"]) <= 30
    assert context["history_chars"] <= 30


def test_gate_context_replaces_prompt():
    context = {"messages": [{"role": "user", "content": "old"}], "prompt": "old"}
    gate = context_for_gate(context, "now")
    assert gate["prompt"] == "now"


def test_response_is_appended_once():
    context = {"messages": [{"role": "user", "content": "q"}], "prompt": "q"}
    with_response = context_with_response(context, "a", tool_names=["say"])
    assert with_response["messages"][-1]["content"] == "a"
    assert with_response["response_tools"] == ["say"]
    again = context_with_response(with_response, "a")
    assert again["messages"][-1]["content"] == "a"
