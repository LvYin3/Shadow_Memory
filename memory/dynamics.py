"""S/R 记忆动力学（纯函数，无 IO）。

S 为存储强度（天），R 为当前可提取度 [0,1]。
"""

from __future__ import annotations

import math

from ..core.config import ConfigView


class Dynamics:
    def __init__(self, config: ConfigView) -> None:
        self.s_decay = config.float("dynamics.s_decay", 0.999, lo=0.9, hi=1.0)
        self.r_floor = config.float("dynamics.r_floor", 0.2, lo=0.0, hi=1.0)
        self.recall_gain = config.float("dynamics.recall_gain", 2.0, lo=0.0, hi=10.0)
        self.gain_cap = config.float("dynamics.gain_cap", 1.5, lo=1.0, hi=5.0)
        self.archive_s = config.float("dynamics.archive_s", 1.0, lo=0.0, hi=100.0)

    # ------------------------------------------------------------------ 写入
    @staticmethod
    def m(salience: int) -> float:
        level = min(5, max(1, int(salience or 2)))
        return 0.5 + 1.5 * (level - 1) / 4.0

    def s0_event(self, salience: int) -> float:
        return 7.0 * self.m(salience)

    def s0_fact(self, salience: int) -> float:
        return 30.0 * self.m(salience)

    # ------------------------------------------------------------------ 衰减
    @staticmethod
    def r(storage_strength: float, last_at_ms: int | None, now_ms: int) -> float:
        strength = max(float(storage_strength or 0.0), 0.1)
        if not last_at_ms:
            return 1.0
        dt_days = max(0.0, (float(now_ms) - float(last_at_ms)) / 86_400_000.0)
        exponent = -dt_days / strength
        value = math.pow(2.0, exponent)
        if not math.isfinite(value):
            return 0.0
        return min(1.0, max(0.0, value))

    def on_recall(self, storage_strength: float, r_before: float, dt_days: float) -> float:
        strength = max(float(storage_strength or 0.0), 0.1)
        r_value = min(1.0, max(0.0, float(r_before)))
        dt = max(0.0, float(dt_days))
        gain = self.recall_gain * (1.0 - r_value) * math.log1p(dt)
        return min(strength * self.gain_cap, strength + gain)

    def decay(self, storage_strength: float) -> float:
        return max(0.0, float(storage_strength or 0.0) * self.s_decay)

    def should_archive(self, storage_strength: float) -> bool:
        return float(storage_strength or 0.0) < self.archive_s
