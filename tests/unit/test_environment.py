from __future__ import annotations

from astrbot_plugin_shadow_memory.interaction.environment import (
    build_environment_prompt,
    platform_label,
    say_example_messages,
)


def make(**overrides):
    data = {
        "platform": "aiocqhttp",
        "chat_type": "group",
        "group_name": "摸鱼群",
        "group_id": "123",
        "sender_name": "小明",
        "rules": "",
    }
    data.update(overrides)
    return build_environment_prompt(**data)


def test_group_prompt_only_describes_scene_and_say():
    text = make()
    assert "我在 QQ（aiocqhttp）上" in text
    assert "正跟小明聊天" in text
    assert "群聊「摸鱼群」" in text
    assert "必须调用 say 函数" in text
    assert "不调用 say" in text
    assert 'say(text="在吗")' in text
    # 不规定该环境下的动作/风格
    assert "群里大家都看着" not in text
    assert "不抢话" not in text
    assert "Markdown" not in text
    assert "你正在" not in text
    assert "你必须" not in text


def test_private_prompt_has_no_group():
    text = make(chat_type="private", group_name="", group_id="")
    assert "私聊" in text
    assert "群聊「" not in text


def test_other_platform_label():
    text = make(platform="telegram", chat_type="private")
    assert "我在 telegram上" in text
    assert platform_label("") == "即时消息"


def test_custom_rules_replace_default():
    text = make(rules="- 我是自定义规则。")
    assert "- 我是自定义规则。" in text
    assert "必须调用 say 函数" not in text
    assert "我在 QQ（aiocqhttp）上" in text


def test_disabled_returns_empty():
    assert make(enabled=False) == ""


def test_group_name_falls_back_to_id():
    text = make(group_name="", group_id="456")
    assert "群聊「456」" in text


def test_compact_prompt_has_only_say_line():
    text = make(compact=True)
    assert "必须调用 say 函数" in text
    assert "# 我怎么把话发出去" not in text
    assert "我在 QQ（aiocqhttp）上" in text


def test_say_example_messages_are_valid_tool_pair():
    assistant, tool = say_example_messages("demo")
    assert assistant["role"] == "assistant"
    assert assistant["content"] == ""
    call = assistant["tool_calls"][0]
    assert call["id"] == "demo"
    assert call["function"]["name"] == "say"
    assert '"text"' in call["function"]["arguments"]
    assert tool == {"role": "tool", "tool_call_id": "demo", "content": "已发送"}
