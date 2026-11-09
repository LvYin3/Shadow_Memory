"""容错 JSON 解析。

模型输出经常带 ```json 围栏、前后解释文字或单引号，这里只做一件事：
从一段文本里尽量抠出第一个可解析的 JSON 值。
"""

from __future__ import annotations

import json
from typing import Any


def _strip_fence(text: str) -> str:
    t = text.strip()
    if "```" not in t:
        return t
    parts = t.split("```")
    for part in parts[1:]:
        body = part.strip()
        if body.lower().startswith("json"):
            body = body[4:].strip()
        if body.startswith("{") or body.startswith("["):
            return body
    return t


def extract_json(text: str | None) -> Any | None:
    """返回解析出的 JSON 值；失败返回 None。"""
    if text is None:
        return None
    if isinstance(text, (dict, list)):
        return text
    raw = str(text).strip().lstrip("\ufeff")
    if not raw:
        return None
    candidates = [raw, _strip_fence(raw)]
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            pass
        for index, char in enumerate(candidate):
            if char not in "{[":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
            except (ValueError, TypeError):
                continue
            if isinstance(value, (dict, list)):
                return value
    return None


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
