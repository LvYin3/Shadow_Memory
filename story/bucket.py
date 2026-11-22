"""唤醒额度：令牌桶（纯函数，无 IO）。

只有两个参数：每天回补多少次、最多攒多少次。没用完的留在桶里，到容量封顶。
"""

from __future__ import annotations

import math

MS_PER_DAY = 86_400_000.0


def initial(*, per_day: float, capacity: float) -> float:
    """第一次开启时给一天的量，不直接给满桶，避免刚开就连演几十场。"""
    return max(0.0, min(float(capacity), float(per_day)))


def refill(
    tokens: float, at_ms: int, now_ms: int, *, per_day: float, capacity: float
) -> float:
    """按经过的时间匀速回补，封顶到容量。"""
    cap = max(0.0, float(capacity))
    current = max(0.0, float(tokens or 0.0))
    if not at_ms or now_ms <= at_ms:
        return min(cap, current)
    gained = (float(now_ms) - float(at_ms)) * max(0.0, float(per_day)) / MS_PER_DAY
    return min(cap, current + gained)


def ms_until(tokens: float, need: float, *, per_day: float) -> int | None:
    """还要多久才能攒到 need；回补速率为 0 时返回 None（永远攒不到）。"""
    missing = float(need) - float(tokens or 0.0)
    if missing <= 0:
        return 0
    rate = float(per_day)
    if rate <= 0:
        return None
    return int(math.ceil(missing * MS_PER_DAY / rate))
