"""时间工具：插件内部一律用毫秒整数时间戳。"""

from __future__ import annotations

import time
from datetime import datetime

MS_PER_DAY = 86_400_000.0


def now_ms() -> int:
    return int(time.time() * 1000)


def ms_to_days(ms: float) -> float:
    return float(ms) / MS_PER_DAY


def days_between(a_ms: int | None, b_ms: int) -> float:
    if not a_ms:
        return 0.0
    return max(0.0, (float(b_ms) - float(a_ms)) / MS_PER_DAY)


def day_index(ms: int | None = None) -> int:
    """本地日历日序号，用于「每日一次」类判断。"""
    stamp = now_ms() if ms is None else ms
    return datetime.fromtimestamp(stamp / 1000.0).toordinal()


def day_key(ms: int | None = None) -> str:
    stamp = now_ms() if ms is None else ms
    return datetime.fromtimestamp(stamp / 1000.0).strftime("%Y-%m-%d")


def iso(ms: int | None = None) -> str:
    stamp = now_ms() if ms is None else ms
    return datetime.fromtimestamp(stamp / 1000.0).isoformat(timespec="seconds")
