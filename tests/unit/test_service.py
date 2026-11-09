from __future__ import annotations

import pytest

from astrbot_plugin_shadow_memory.core.types import Turn
from astrbot_plugin_shadow_memory.interaction.commands import command_args

from ..fakes import FakeJev
from .helpers import build_stack, text_of_len

DAY = 86_400_000


def test_command_args_variants():
    assert command_args("memory", "memory") == ""
    assert command_args("/memory", "memory") == ""
    assert command_args("memory export seed", "memory") == "export seed"
    assert command_args("/memory import a.json force", "memory") == "import a.json force"
    assert command_args("forget 42", "forget") == "42"
    assert command_args("qzone 今天风很好，想发一条说说", "qzone") == "今天风很好，想发一条说说"
    assert command_args("", "memory") == ""
    assert command_args("memoryx", "memory") == "memoryx"


@pytest.fixture
def stack(db, configs):
    return build_stack(db, configs)


async def _observe(stack, text: str, ts: int):
    await stack.service.observe(
        Turn("bot1", "umo1", "user", text, ts, "u1", "小明"),
        display_name="测试群",
        chat_type="group",
    )


async def test_prepare_turn_falls_back_to_fact(stack):
    now = 1_800_000_000_000
    await stack.facts.add_manual("bot1", "小明住在杭州", salience=3, now=now)
    prepared = await stack.service.prepare_turn("bot1", "umo1", "杭州最近怎么样", [], now)
    assert prepared.parts == ["小明住在杭州"]
    assert prepared.fact_ids
    row = await stack.db.fetchone("SELECT last_recalled_at FROM facts")
    assert row["last_recalled_at"] == now


async def test_post_turn_defaults_when_judge_fails(stack):
    stack.jev.available = False
    post = await stack.service.post_turn(
        bot_id="bot1", umo="umo1", user_text="随便聊聊", assistant_text="嗯"
    )
    assert post.salience == 2
    assert post.durable_fact == 0.0
    assert post.closed is False


async def test_post_turn_adds_tension_on_conflict(stack):
    now = 1_800_000_000_000
    await _observe(stack, "那天你为什么先走", now)
    stack.jev.ask_value = {
        "salience": {"score": 3.0},
        "conflict": {"noul": 0.9},
        "durable_fact": {"noul": 0.1},
        "topic_end": {"noul": 0.1},
    }
    stack.text.push("我还是没想明白，那天你为什么先走。")
    post = await stack.service.post_turn(
        bot_id="bot1", umo="umo1", user_text="那天你为什么先走", assistant_text="我不想说"
    )
    assert post.tension_added is True
    counts = await stack.persona.counts("bot1")
    assert counts["tension"] == 1
    assert stack.text.tasks[-1] == "tension"


async def test_post_turn_queues_durable_fact(stack):
    now = 1_800_000_000_000
    await _observe(stack, "我下周三去北京", now)
    stack.jev.ask_value = {
        "salience": {"score": 3.0},
        "conflict": {"noul": 0.1},
        "durable_fact": {"noul": 0.9},
        "topic_end": {"noul": 0.1},
    }
    post = await stack.service.post_turn(
        bot_id="bot1",
        umo="umo1",
        user_text="我下周三去北京",
        assistant_text="好",
        speaker_name="小明",
    )
    assert post.durable_fact == pytest.approx(0.9)
    pending = await stack.facts.pending("bot1")
    assert len(pending) == 1
    assert pending[0]["text"].startswith("小明: 我下周三去北京")


async def test_post_turn_tension_fallback_names_the_other_party(stack):
    now = 1_800_000_000_000
    await _observe(stack, "那天你为什么先走", now)
    stack.jev.ask_value = {
        "salience": {"score": 3.0},
        "conflict": {"noul": 0.9},
        "durable_fact": {"noul": 0.1},
        "topic_end": {"noul": 0.1},
    }
    stack.text.push("")
    await stack.service.post_turn(
        bot_id="bot1",
        umo="umo1",
        user_text="那天你为什么先走",
        assistant_text="我不想说",
        speaker_name="小明",
    )
    row = await stack.db.fetchone(
        "SELECT text FROM persona_entries WHERE layer='tension' ORDER BY id DESC LIMIT 1"
    )
    assert "小明说的" in row["text"]


async def test_post_turn_closes_topic(stack):
    now = 1_800_000_000_000
    await _observe(stack, "晚安", now)
    stack.jev.ask_value = {
        "salience": {"score": 2.0},
        "conflict": {"noul": 0.1},
        "durable_fact": {"noul": 0.1},
        "topic_end": {"noul": 0.9},
    }
    stack.text.push({"fragment": "那天聊到很晚", "entities": []})
    post = await stack.service.post_turn(
        bot_id="bot1", umo="umo1", user_text="晚安", assistant_text="晚安，睡个好觉。"
    )
    assert post.closed is True


async def test_forget_deactivates_orphan_persona_entry(stack):
    now = 1_800_000_000_000
    await _observe(stack, "一件会被忘掉的事", now)
    await stack.db.execute(
        "INSERT INTO events(id, bot_id, umo, kind, started_at, ended_at, fragment, entities, "
        "storage_strength, retrievability, salience, status, created_at) "
        "VALUES(99,'bot1','umo1','episodic',?,?,'fragment','[]',7,1,3,'closed',?)",
        (now, now, now),
    )
    await stack.persona.apply_ops(
        "bot1",
        [
            {
                "op": "add",
                "layer": "edge",
                "text": text_of_len(60, "我在被问到那件事时"),
                "source_event_ids": [99],
            }
        ],
    )
    assert await stack.service.forget("bot1", 99) is True
    assert await stack.persona.entries("bot1") == []


async def test_summary_shape(stack):
    now = 1_800_000_000_000
    await _observe(stack, "随便说说", now)
    summary = await stack.service.summary("bot1")
    for key in ("bot_id", "persona", "events", "sr", "edges", "facts", "inject_24h", "gate_24h"):
        assert key in summary
    assert summary["bot_id"] == "bot1"
