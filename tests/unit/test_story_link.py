"""第二段的纯函数：远方动作、安静时段、唤醒时间和消息种类。"""

from __future__ import annotations

import datetime

from astrbot_plugin_shadow_memory.story import link


def test_remote_pick_takes_the_highest_and_refuses_a_tie():
    assert link.pick_remote({"parcel": 0.2, "food": 0.9, "money": 0.4}, 0.6) == "food"
    assert link.pick_remote({"parcel": 0.8, "food": 0.8, "money": 0.1}, 0.6) == ""
    assert link.pick_remote({"parcel": None, "food": 0.5, "money": 0.4}, 0.6) == ""


def test_quiet_hours_can_cross_midnight():
    evening = int(datetime.datetime(2026, 11, 23, 23, 30).timestamp() * 1000)
    noon = int(datetime.datetime(2026, 11, 23, 12, 0).timestamp() * 1000)
    assert link.quiet_active("23:00-08:00", evening) is True
    assert link.quiet_active("23:00-08:00", noon) is False
    assert link.quiet_active("", noon) is False


def test_wake_zero_is_immediate_and_blank_asks_again():
    assert link.wake_request(0) == ("at", 0)
    assert link.wake_request(None) == ("ask", 0)
    assert link.wake_request("abc") == ("ask", 0)
    assert link.wake_request(-3) == ("ask", 0)
    assert link.clamp_wake(1, min_wake=10, max_wake=30) == 10
    assert link.clamp_wake(1, min_wake=0, max_wake=0) == 1


def test_outbound_kind_prefers_a_settled_intent_over_unread():
    assert link.classify_outbound("umo", settled={"umo"}, unread={"umo"}) == "report"
    assert link.classify_outbound("umo", settled=set(), unread={"umo"}) == "catchup"
    assert link.classify_outbound("umo", settled=set(), unread=set()) == "share"
