from __future__ import annotations

from astrbot_plugin_shadow_memory.core.config import ConfigView
from astrbot_plugin_shadow_memory.interaction.context_inject import (
    builtin_group_context_enabled,
    format_block,
    missing_for_contexts,
    normalize_text,
    resolve_limit,
    trim_to_chars,
    without_current,
)


def msg(role: str, text: str, speaker: str = "") -> dict:
    return {"role": role, "text": text, "speaker": speaker}


def test_normalize_text():
    assert normalize_text("  a\n\tb  ") == "a b"
    assert normalize_text(None) == ""


def test_trim_to_chars_keeps_newest():
    rows = [msg("user", "a" * 10), msg("assistant", "b" * 10), msg("user", "c" * 10)]
    kept = trim_to_chars(rows, 25)
    assert [item["text"] for item in kept] == ["b" * 10, "c" * 10]


def test_trim_single_long_message():
    kept = trim_to_chars([msg("user", "x" * 100)], 10)
    assert kept[0]["text"] == "x" * 10


def test_without_current_removes_last_match():
    rows = [msg("user", "在吗"), msg("user", "在吗"), msg("assistant", "在")]
    kept = without_current(rows, "在吗")
    assert [item["text"] for item in kept] == ["在吗", "在"]


def test_missing_for_contexts_dedupes():
    contexts = [{"role": "user", "content": "在吗"}]
    recent = [msg("user", "在吗"), msg("user", "今天风大"), msg("assistant", "嗯")]
    missing = missing_for_contexts(contexts, recent)
    assert missing == [
        {"role": "user", "content": "今天风大"},
        {"role": "assistant", "content": "嗯"},
    ]


def test_format_block_labels():
    block = format_block(
        [msg("user", "在吗", "小明"), msg("assistant", "在")], 2000
    )
    assert block.startswith("【最近会话】")
    assert "用户（小明）：在吗" in block
    assert "你：在" in block


def test_builtin_group_context_enabled():
    assert builtin_group_context_enabled({"group_icl_enable": True}) is True
    assert builtin_group_context_enabled({"active_reply": {"enable": True}}) is True
    assert builtin_group_context_enabled({"active_reply": {"enable": False}}) is False
    assert builtin_group_context_enabled(None) is False


def test_resolve_limit_override_and_fallback():
    cfg = ConfigView({"context": {"recent_messages": 20}}, "")
    assert resolve_limit(cfg, None) == 20
    assert resolve_limit(cfg, "") == 20
    assert resolve_limit(cfg, "8") == 8
    assert resolve_limit(cfg, "0") == 0
    assert resolve_limit(cfg, "999") == 50
    assert resolve_limit(cfg, "bad") == 20
