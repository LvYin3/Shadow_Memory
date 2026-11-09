from __future__ import annotations

import pytest

from astrbot_plugin_shadow_memory.core.config import ConfigView
from astrbot_plugin_shadow_memory.memory.dynamics import Dynamics

DAY = 86_400_000


@pytest.fixture
def dyn():
    return Dynamics(ConfigView({}, "bot"))


def test_salience_multiplier(dyn):
    assert dyn.m(1) == 0.5
    assert dyn.m(3) == 1.25
    assert dyn.m(5) == 2.0


def test_event_s0_five_levels(dyn):
    values = [dyn.s0_event(level) for level in range(1, 6)]
    assert values == pytest.approx([3.5, 6.125, 8.75, 11.375, 14.0])


def test_fact_s0_is_thirty_days_based(dyn):
    assert dyn.s0_fact(1) == pytest.approx(15.0)
    assert dyn.s0_fact(5) == pytest.approx(60.0)


def test_r_curve_matches_formula(dyn):
    strength = 8.75
    now = 10 * DAY
    r = dyn.r(strength, now - 7 * DAY, now)
    assert r == pytest.approx(2 ** (-7 / 8.75), abs=0.001)


def test_r_is_one_without_last_recall(dyn):
    assert dyn.r(5.0, None, 1) == 1.0


def test_r_clamped_for_absurd_inputs(dyn):
    assert dyn.r(0.0, 0, DAY) == 1.0
    assert 0.0 <= dyn.r(1.0, 0, 10**6 * DAY) <= 1.0


def test_interval_effect_grows_more_than_massed_repetition(dyn):
    strength = 8.75
    now = 100 * DAY
    # 间隔 7 天后想起
    last = now - 7 * DAY
    r_before = dyn.r(strength, last, now)
    spaced = dyn.on_recall(strength, r_before, 7.0)
    # 同一晚每 10 分钟复述一次
    r_massed = dyn.r(strength, now - 10 * 60_000, now)
    massed = dyn.on_recall(strength, r_massed, 10 * 60_000 / DAY)
    assert (spaced - strength) >= 1.5 * (massed - strength)


def test_gain_capped_at_half_again(dyn):
    strength = 10.0
    grown = dyn.on_recall(strength, 0.0, 365.0)
    assert grown == pytest.approx(15.0)


def test_daily_decay_and_archive(dyn):
    assert dyn.decay(10.0) == pytest.approx(9.99)
    assert dyn.should_archive(0.99) is True
    assert dyn.should_archive(1.0) is False
