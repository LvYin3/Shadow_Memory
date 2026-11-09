from __future__ import annotations

import pytest

from astrbot_plugin_shadow_memory.core import clock as clock_mod
from astrbot_plugin_shadow_memory.core.types import Turn

from ..unit.helpers import build_stack, text_of_len

DAY = 86_400_000


@pytest.fixture
def stack(db, configs):
    return build_stack(db, configs)


async def _observe(stack, text: str, ts: int):
    await stack.service.observe(
        Turn("bot1", "umo1", "user", text, ts, "u1", "小明"),
        display_name="测试群",
        chat_type="group",
    )


async def test_full_memory_cycle(stack):
    now = clock_mod.now_ms() - 10 * DAY
    for index, body in enumerate(["去了海边", "风很大", "下次还想去"]):
        await _observe(stack, body, now + index * 1000)
    stack.text.push(
        {"fragment": "那天我和小明去了海边，风很大但我很喜欢", "entities": ["海边", "小明", "风"]}
    )
    closed = await stack.segmenter.close_event(1)
    assert closed["fragment"]

    prepared = await stack.service.prepare_turn("bot1", "umo1", "还记得海边吗", [], now + 5000)
    assert prepared.parts, prepared.reason
    assert prepared.injection is not None
    assert len(prepared.parts) <= 2

    for index, body in enumerate(["又聊到海边", "还是那阵风", "小明也在"]):
        await _observe(stack, body, now + 90_000_000 + index * 1000)
    stack.text.push({"fragment": "后来又说起海边那阵风", "entities": ["海边", "风", "小明"]})
    await stack.segmenter.close_event(2)
    for index, body in enumerate(["第三次说起风", "我记得很清楚", "那天的光"]):
        await _observe(stack, body, now + 180_000_000 + index * 1000)
    stack.text.push({"fragment": "第三次说起那阵风的时候，我还是记得", "entities": ["海边", "风", "小明"]})
    await stack.segmenter.close_event(3)

    post = await stack.service.post_turn(
        bot_id="bot1", umo="umo1", user_text="那天的光", assistant_text="嗯，我也记得"
    )
    assert post.salience >= 1
    assert post.durable_fact >= 0.6
    assert len(await stack.facts.pending("bot1")) == 1

    stack.text.push(
        {
            "facts": [
                {"kind": "profile", "text": "小明喜欢海边", "key_fact_type": "none", "salience": 3}
            ]
        }
    )
    assert await stack.service.extract_facts("bot1") == 1

    stack.text.push(
        {
            "gist_fragment": "海边和小明的那阵风，我一直记着",
            "gist_summary": "几次聊到海边与风，小明都在。",
            "entities": ["海边", "风", "小明"],
            "supersede": [],
            "insights": ["我发现我很在意和小明一起经历的小事"],
        }
    )
    consolidation = await stack.service.consolidate("bot1", now + 181_000_000)
    assert consolidation.merged == 1
    gist_id = await stack.db.scalar("SELECT id FROM events WHERE kind='gist'")

    stack.text.push(
        {
            "ops": [
                {
                    "op": "add",
                    "layer": "core",
                    "text": text_of_len(60, "我慢慢发现"),
                    "source_event_ids": [gist_id],
                }
            ]
        }
    )
    distill = await stack.service.distill("bot1", "manual")
    assert distill.applied == 1
    persona_text = await stack.persona.render("bot1")
    assert persona_text.startswith("我慢慢发现")

    summary = await stack.service.summary("bot1")
    assert summary["events"]
    assert summary["persona"]["chars"] > 0


async def test_two_bots_do_not_share_memory(stack):
    now = clock_mod.now_ms() - DAY
    await _observe(stack, "只属于 bot1 的事", now)
    stack.text.push({"fragment": "bot1 的秘密", "entities": ["秘密"]})
    await stack.segmenter.close_event(1)

    candidates, _ = await stack.recall.candidates("bot2", "秘密", now + 1000)
    assert candidates == []
    assert await stack.persona.render("bot2") == ""
    assert await stack.db.scalar("SELECT COUNT(*) FROM events WHERE bot_id='bot2'") == 0


async def test_injection_parts_are_bounded(stack):
    now = clock_mod.now_ms() - DAY
    await _observe(stack, "海边风很大", now)
    stack.text.push({"fragment": "很长的碎片" * 20, "entities": ["海边"]})
    await stack.segmenter.close_event(1)
    prepared = await stack.service.prepare_turn("bot1", "umo1", "海边", [], now + 1000)
    assert all(len(part) <= 40 for part in prepared.parts)


async def test_prepare_turn_respects_veto(stack):
    prepared = await stack.service.prepare_turn(
        "bot1", "umo1", "/help", [], veto_reason="veto"
    )
    assert prepared.reason == "veto"
    assert prepared.parts == []


async def test_forget_removes_event_and_cascades(stack):
    now = clock_mod.now_ms() - DAY
    await _observe(stack, "要忘掉的事", now)
    stack.text.push({"fragment": "一段该忘掉的经历", "entities": ["忘记"]})
    await stack.segmenter.close_event(1)
    await stack.db.execute(
        "INSERT INTO event_turns(event_id, seq, role, text, ts) VALUES(1,1,'user','x',1)"
    )
    assert await stack.service.forget("bot1", 1) is True
    assert await stack.db.scalar("SELECT COUNT(*) FROM events") == 0
    assert await stack.db.scalar("SELECT COUNT(*) FROM event_turns") == 0
    assert await stack.db.scalar("SELECT COUNT(*) FROM event_edges") == 0
