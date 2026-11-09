"""固定聊天上下文的纯函数：裁剪、去重、格式化、合并。"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from ..core.config import ConfigView

_WS = re.compile(r"\s+")


def normalize_text(value: Any) -> str:
    return _WS.sub(" ", str(value or "")).strip()


def content_text(message: Any) -> str:
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return normalize_text(content)
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, Mapping):
                parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(part or ""))
        return normalize_text(" ".join(parts))
    return normalize_text(content)


def resolve_limit(cfg: ConfigView, override: Any = None) -> int:
    """每 bot 覆盖优先；空/缺失跟随全局；非法回退全局。"""
    raw = "" if override is None else str(override).strip()
    if raw:
        try:
            return max(0, min(50, int(raw)))
        except (TypeError, ValueError):
            pass
    return cfg.int("context.recent_messages", 20, lo=0, hi=50)


def trim_to_chars(recent: Sequence[Mapping[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    if max_chars <= 0:
        return [dict(item) for item in recent]
    kept: list[dict[str, Any]] = []
    total = 0
    for item in reversed(list(recent)):
        body = str(item.get("text") or "")
        if kept and total + len(body) > max_chars:
            break
        if not kept and len(body) > max_chars:
            item = dict(item)
            item["text"] = body[-max_chars:]
            body = item["text"]
        kept.append(dict(item))
        total += len(body)
    return list(reversed(kept))


def without_current(
    recent: Sequence[Mapping[str, Any]], current_text: str
) -> list[dict[str, Any]]:
    current = normalize_text(current_text)
    rows = [dict(item) for item in recent]
    if not current:
        return rows
    for index in range(len(rows) - 1, -1, -1):
        if str(rows[index].get("role")) == "user" and normalize_text(
            rows[index].get("text")
        ) == current:
            rows.pop(index)
            break
    return rows


def missing_for_contexts(
    contexts: Sequence[Any], recent: Sequence[Mapping[str, Any]]
) -> list[dict[str, str]]:
    existing = {
        normalize_text(content_text(message))
        for message in contexts
        if isinstance(message, Mapping)
    }
    missing: list[dict[str, str]] = []
    for item in recent:
        text = normalize_text(item.get("text"))
        if not text or text in existing:
            continue
        existing.add(text)
        role = str(item.get("role") or "user")
        missing.append({"role": role if role in {"user", "assistant"} else "user", "content": text})
    return missing


def format_block(
    recent: Sequence[Mapping[str, Any]], max_chars: int = 2000
) -> str:
    rows = trim_to_chars(recent, max_chars)
    if not rows:
        return ""
    lines = ["【最近会话】"]
    for item in rows:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        role = str(item.get("role") or "user")
        if role == "assistant":
            lines.append(f"你：{text}")
        else:
            speaker = str(item.get("speaker") or "").strip()
            lines.append(f"用户（{speaker}）：{text}" if speaker else f"用户：{text}")
    return "\n".join(lines) if len(lines) > 1 else ""


def builtin_group_context_enabled(settings: Any) -> bool:
    """与 AstrBot 内置 group_context_enabled 对齐。"""
    if not isinstance(settings, Mapping):
        return False
    if bool(settings.get("group_icl_enable")):
        return True
    active = settings.get("active_reply")
    return isinstance(active, Mapping) and bool(active.get("enable"))
