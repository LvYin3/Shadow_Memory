from __future__ import annotations

from astrbot_plugin_shadow_memory.memory.recall_format import (
    MEMORY_OWNERSHIP_PROMPT,
    format_recall_block,
)


def test_self_recall_block_marks_experience_and_aftertone():
    block = format_recall_block(["爸妈支持我喜欢的东西", "那天海边的风很大"])
    assert block.startswith("\n\n【我的记忆，不是对方说的】\n")
    assert "我想起：爸妈支持我喜欢的东西" in block
    assert "我心里还留着：那天海边的风很大" in block
    assert "我知道：" not in block


def test_fact_recall_block_marks_knowledge_not_experience():
    block = format_recall_block(["小明在杭州"], fact=True)
    assert block.startswith("\n\n【我知道的事，不是对方刚说的】\n")
    assert "我知道：小明在杭州" in block
    assert "我想起：" not in block
    assert "我心里还留着：" not in block


def test_empty_parts_produce_no_block():
    assert format_recall_block([]) == ""
    assert format_recall_block(["", "   "]) == ""


def test_ownership_prompt_is_first_person_without_user_word_or_commands():
    assert "用户" not in MEMORY_OWNERSHIP_PROMPT
    assert "不要" not in MEMORY_OWNERSHIP_PROMPT
    assert "必须" not in MEMORY_OWNERSHIP_PROMPT
    assert "【我的记忆，不是对方说的】" in MEMORY_OWNERSHIP_PROMPT
    assert "【我知道的事，不是对方刚说的】" in MEMORY_OWNERSHIP_PROMPT
    assert "不会出现在我说的话里" in MEMORY_OWNERSHIP_PROMPT
