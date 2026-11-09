"""记忆归属：固定说明与召回块格式化。"""

from __future__ import annotations

MEMORY_OWNERSHIP_PROMPT = """# 关于我的记忆
- 【我的记忆，不是对方说的】是我自己亲历或想起的事；里面的“我”是我。
- 【我知道的事，不是对方刚说的】是我知道的信息，不是刚刚听对方说的。
- 这些标签只是给我自己看的，不会出现在我说的话里。"""

SELF_HEADER = "【我的记忆，不是对方说的】"
FACT_HEADER = "【我知道的事，不是对方刚说的】"


def format_recall_block(parts: list[str], *, fact: bool = False) -> str:
    """把召回片段合成一个 user 消息末尾的临时块；事实与自身经历分开标注。"""
    cleaned = [part.strip() for part in parts if part and part.strip()]
    if not cleaned:
        return ""
    if fact:
        header = FACT_HEADER
        lines = [f"我知道：{part}" for part in cleaned]
    else:
        header = SELF_HEADER
        lines = [f"我想起：{cleaned[0]}"]
        lines.extend(f"我心里还留着：{part}" for part in cleaned[1:])
    return "\n\n" + "\n".join([header, *lines])
