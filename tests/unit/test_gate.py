from __future__ import annotations

import pytest

from .helpers import build_stack, text_of_len
from ..fakes import FakeEvent


@pytest.fixture
def stack(db, configs):
    return build_stack(db, configs)


async def test_gate_passes_high_probability(stack):
    stack.jev.noul_value = 0.9
    decision = await stack.gate.judge(
        bot_id="bot1", umo="umo1", message="有人吗", sender_name="小明"
    )
    assert decision.passed is True
    assert decision.probability == pytest.approx(0.9)
    rows = await stack.db.fetchall("SELECT decision, reason FROM gate_logs")
    assert rows[0]["decision"] == "reply"


async def test_gate_stays_silent_below_threshold(stack):
    stack.jev.noul_value = 0.2
    decision = await stack.gate.judge(
        bot_id="bot1", umo="umo1", message="今天天气", sender_name="小明"
    )
    assert decision.passed is False
    assert decision.reason == "below_threshold"


async def test_gate_backs_off_after_three_failures(stack):
    stack.jev.noul_value = None
    for _ in range(3):
        decision = await stack.gate.judge(
            bot_id="bot1", umo="umo1", message="随便", sender_name="小明"
        )
        assert decision.reason == "judge_failed"
    calls_before = len(stack.jev.calls)
    decision = await stack.gate.judge(
        bot_id="bot1", umo="umo1", message="再试一次", sender_name="小明"
    )
    assert decision.reason == "judge_failed"
    assert len(stack.jev.calls) == calls_before  # 退避期内不再询问


async def test_gate_disabled(stack, make_configs):
    local = build_stack(stack.db, make_configs({"interaction": {"gate_enabled": False}}))
    decision = await local.gate.judge(
        bot_id="bot1", umo="umo1", message="hi", sender_name="小明"
    )
    assert decision.passed is False
    assert decision.reason == "disabled"


async def test_gate_without_provider(stack):
    stack.jev.available = False
    decision = await stack.gate.judge(
        bot_id="bot1", umo="umo1", message="hi", sender_name="小明"
    )
    assert decision.reason == "no_provider"


async def test_gate_state_contains_persona_tendencies(stack):
    await stack.persona.apply_ops(
        "bot1",
        [
            {
                "op": "add",
                "layer": "core",
                "text": text_of_len(60, "我在群里更喜欢先听别人把话说完，再接最轻的那一句。"),
                "source_event_ids": [1],
            }
        ],
    )
    stack.jev.noul_value = 0.9
    await stack.gate.judge(bot_id="bot1", umo="umo1", message="说话", sender_name="小明")
    _, instructions = stack.jev.calls[-1]
    assert instructions  # 确实问了 reply_now


async def test_note_reply_tracks_time(stack):
    stack.gate.note_reply("bot1", "umo1", 12345)
    assert stack.gate._last_reply[("bot1", "umo1")] == 12345


def test_fake_event_contract():
    event = FakeEvent(text="你好", wake=False)
    assert event.get_message_type() == "GroupMessage"
    assert event.unified_msg_origin == "aiocqhttp:GroupMessage:g1"
    assert event.is_private_chat() is False
