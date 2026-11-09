"""最终文本是否丢弃的纯规则。"""

from __future__ import annotations


def should_drop_final_text(*, sent: bool, silent: bool, fallback: bool) -> bool:
    """本轮已经用 say/speak 说过，或者严格沉默（未开兜底）时，丢弃最终文本。"""
    if sent:
        return True
    if silent:
        return True
    return not fallback
