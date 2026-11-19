"""对话请求的文本等价快照，供 JEV 判断使用。"""

from __future__ import annotations

import copy
import json
from typing import Any

DEFAULT_MAX_HISTORY_CHARS = 16_000


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
                elif item.get("type") in {"image", "image_url", "audio", "record"}:
                    parts.append(f"[{item.get('type')}]")
        return "\n".join(parts)
    if isinstance(value, dict):
        text = value.get("text")
        return text if isinstance(text, str) else json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _tool_calls(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    calls: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                pass
        calls.append({"name": name, "arguments": arguments})
    return calls


def _normalize_message(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    role = str(message.get("role") or "").strip()
    if role not in {"system", "user", "assistant", "tool"}:
        return None
    normalized: dict[str, Any] = {
        "role": role,
        "content": _text(message.get("content")),
    }
    calls = _tool_calls(message.get("tool_calls"))
    if calls:
        normalized["tool_calls"] = calls
    if message.get("name"):
        normalized["name"] = str(message["name"])
    return normalized


def _message_chars(message: dict[str, Any]) -> int:
    total = len(str(message.get("content") or ""))
    if message.get("tool_calls"):
        total += len(json.dumps(message["tool_calls"], ensure_ascii=False, default=str))
    if message.get("name"):
        total += len(str(message["name"]))
    return total


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 8:
        return text[-limit:]
    head = max(1, int(limit * 0.7))
    tail = max(1, limit - head - 1)
    return text[:head] + "…" + text[-tail:]


def _turn_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "user" and current:
            groups.append(current)
            current = [message]
        else:
            current.append(message)
    if current:
        groups.append(current)
    return groups


def _trim_turn(turn: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    remaining = max(0, budget)
    for message in reversed(turn):
        item = copy.deepcopy(message)
        chars = _message_chars(item)
        if chars <= remaining:
            kept.insert(0, item)
            remaining -= chars
            continue
        if not kept:
            item["content"] = _truncate_text(str(item.get("content") or ""), remaining)
            item.pop("tool_calls", None)
            kept.append(item)
        break
    return kept


def _select_history(
    messages: list[dict[str, Any]], budget: int
) -> tuple[list[dict[str, Any]], int, bool]:
    if budget <= 0:
        return [], 0, bool(messages)
    groups = _turn_groups(messages)
    selected: list[dict[str, Any]] = []
    remaining = budget
    truncated = False
    for turn in reversed(groups):
        chars = sum(_message_chars(message) for message in turn)
        if chars <= remaining:
            selected = copy.deepcopy(turn) + selected
            remaining -= chars
            continue
        if not selected:
            trimmed = _trim_turn(turn, remaining)
            selected = trimmed
            truncated = True
        else:
            truncated = True
        break
    return selected, sum(_message_chars(message) for message in selected), truncated


def build_conversation(
    request: Any,
    *,
    max_history_chars: int = DEFAULT_MAX_HISTORY_CHARS,
) -> dict[str, Any]:
    """从 AstrBot ProviderRequest 生成 JEV 可用的文本快照。"""
    raw_messages = getattr(request, "contexts", None) or []
    messages: list[dict[str, Any]] = []
    if isinstance(raw_messages, str):
        try:
            raw_messages = json.loads(raw_messages)
        except (TypeError, ValueError):
            raw_messages = []
    for raw in raw_messages if isinstance(raw_messages, list) else []:
        normalized = _normalize_message(raw)
        if normalized is not None:
            messages.append(normalized)
    selected, history_chars, truncated = _select_history(
        messages, max(0, int(max_history_chars))
    )
    extra_text: list[str] = []
    for part in getattr(request, "extra_user_content_parts", None) or []:
        text = _text(getattr(part, "text", None) or (part if isinstance(part, str) else None))
        if text.strip():
            extra_text.append(text)
    return {
        "system_prompt": str(getattr(request, "system_prompt", "") or ""),
        "messages": selected,
        "prompt": str(getattr(request, "prompt", "") or ""),
        "extra_text": extra_text,
        "history_chars": int(history_chars),
        "truncated": bool(truncated),
    }


def context_with_response(
    context: dict[str, Any] | None,
    text: str,
    *,
    tool_names: list[str] | None = None,
) -> dict[str, Any] | None:
    if not context:
        return None
    snapshot = copy.deepcopy(context)
    messages = snapshot.setdefault("messages", [])
    body = str(text or "").strip()
    if body:
        if not (
            messages
            and messages[-1].get("role") == "assistant"
            and messages[-1].get("content") == body
        ):
            messages.append({"role": "assistant", "content": body})
    if tool_names:
        snapshot["response_tools"] = list(tool_names)[:20]
    return snapshot


def context_for_gate(
    context: dict[str, Any] | None,
    current_message: str,
) -> dict[str, Any] | None:
    if not context:
        return None
    snapshot = copy.deepcopy(context)
    snapshot["prompt"] = str(current_message or "")
    return snapshot
