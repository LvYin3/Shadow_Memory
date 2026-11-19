"""清理模型偶发带入的 `<text>` 包裹/末尾闭合标签。"""

from __future__ import annotations

import re

_PAIR = re.compile(r"^\s*<text>(.*?)</text>\s*$", re.IGNORECASE | re.DOTALL)
_TRAILING = re.compile(r"(?:\s*</text>)\s*$", re.IGNORECASE)


def clean_text_tag(value: str) -> tuple[str, bool]:
    """只处理整个 `<text>...</text>` 或末尾的 `</text>`，不碰正文中间标签。"""
    text = str(value or "").strip()
    changed = False
    for _ in range(4):
        pair = _PAIR.match(text)
        if pair is not None:
            text = pair.group(1).strip()
            changed = True
            continue
        stripped = _TRAILING.sub("", text).rstrip()
        if stripped != text:
            text = stripped
            changed = True
            continue
        break
    return text, changed
