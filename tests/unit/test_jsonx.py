from __future__ import annotations

from astrbot_plugin_shadow_memory.core.jsonx import extract_json


def test_plain_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_fenced_json():
    text = '好的，结果如下：\n```json\n{"fragment": "海边的风"}\n```\n'
    assert extract_json(text) == {"fragment": "海边的风"}


def test_json_with_leading_prose_and_trailing_text():
    text = '我想了想 {"ops": []} 大概就这样'
    assert extract_json(text) == {"ops": []}


def test_array():
    assert extract_json("[1, 2, 3]") == [1, 2, 3]


def test_invalid_returns_none():
    assert extract_json("完全不是 JSON") is None
    assert extract_json("") is None
    assert extract_json(None) is None


def test_already_parsed_passthrough():
    payload = {"answers": {"q": {"noul": 0.4}}}
    assert extract_json(payload) == payload


def test_nested_braces():
    text = '前缀 {"a": {"b": {"c": 1}}} 后缀'
    assert extract_json(text) == {"a": {"b": {"c": 1}}}
