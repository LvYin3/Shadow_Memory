from __future__ import annotations

from astrbot_plugin_shadow_memory.interaction.text_clean import clean_text_tag


def test_removes_trailing_closing_tag():
    assert clean_text_tag("你好</text>") == ("你好", True)


def test_unwraps_whole_pair():
    assert clean_text_tag("<text>你好</text>") == ("你好", True)


def test_removes_multiple_trailing_tags():
    assert clean_text_tag("你好</text></text>") == ("你好", True)


def test_case_insensitive():
    assert clean_text_tag("你好</TEXT>") == ("你好", True)


def test_middle_tag_is_untouched():
    assert clean_text_tag("我说 </text> 不是标签") == ("我说 </text> 不是标签", False)


def test_plain_text_unchanged():
    assert clean_text_tag("普通消息") == ("普通消息", False)


def test_only_tag_becomes_empty():
    assert clean_text_tag("</text>") == ("", True)
