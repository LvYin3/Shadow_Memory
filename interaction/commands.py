"""命令参数解析：不依赖 AstrBot 的 GreedyStr 参数机制。

AstrBot 的 CommandFilter 把带默认值的参数当普通字符串，只取第一个词；
所以命令处理器自己从 ``message_str`` 里剥掉命令名，保留剩余原文。
"""

from __future__ import annotations


def command_args(message_str: str | None, name: str) -> str:
    raw = (message_str or "").strip()
    if raw.startswith(("/", "／")):
        raw = raw[1:].lstrip()
    tokens = raw.split(None, 1)
    if tokens and tokens[0].lower() == name.lower():
        return tokens[1].strip() if len(tokens) > 1 else ""
    return raw
