from __future__ import annotations

import pytest

from astrbot_plugin_shadow_memory.core.types import Turn

from .helpers import build_stack

DAY = 86_400_000


async def _observe(stack, text: str, ts: int, *, role: str = "user", name: str = "小明"):
    return await stack.service.observe(
        Turn("bot1", "umo1", role, text, ts, "u1", name),
        display_name="测试群",
        chat_type="group",
    )


@pytest.fixture
def stack(db, configs):
    return build_stack(db, configs)


async def test_observe_creates_single_open_event(stack):
    base = 1_700_000_000_000
    await _observe(stack, "今天去了海边", base)
    await _observe(stack, "风很大", base + 1000)
    events = await stack.db.fetchall("SELECT * FROM events")
    assert len(events) == 1
    assert events[0]["status"] == "open"
    assert events[0]["turn_count"] == 2
    turns = await stack.db.fetchall("SELECT * FROM event_turns ORDER BY seq")
    assert [row["text"] for row in turns] == ["今天去了海边", "风很大"]


async def test_idle_gap_closes_previous_event(stack):
    base = 1_700_000_000_000
    stack.text.push({"fragment": "那天去了海边", "entities": ["海边"]})
    await _observe(stack, "今天去了海边", base)
    await _observe(stack, "隔了很久再说一句", base + 31 * 60_000)
    events = await stack.db.fetchall("SELECT * FROM events ORDER BY id")
    assert [row["status"] for row in events] == ["closed", "open"]


async def test_close_event_writes_fragment_entities_and_s0(stack):
    base = 1_700_000_000_000
    for index, body in enumerate(["去海边了", "风很大", "下次还想去"]):
        await _observe(stack, body, base + index * 1000)
    stack.text.push(
        {"fragment": "那天我在海边站了很久，风很大", "entities": ["海边", "风", "小明", "海边"]}
    )
    closed = await stack.segmenter.close_event(1, reason="test")
    assert closed["fragment"].startswith("那天我在海边")
    event = await stack.db.fetchone("SELECT * FROM events WHERE id=1")
    assert event["status"] == "closed"
    assert event["storage_strength"] == pytest.approx(6.125)
    assert event["retrievability"] == 1.0
    assert event["entities"] == '["海边", "风", "小明"]'
    assert stack.text.tasks[-1] == "fragment"


async def test_close_event_falls_back_to_attributed_user_message(stack):
    base = 1_700_000_000_000
    await _observe(stack, "这是一句会被截断的兜底碎片", base)
    result = await stack.segmenter.close_event(1, reason="test")
    assert result["fragment"] == "小明说：这是一句会被截断的兜底碎片"


async def test_close_event_prompt_forbids_user_events_as_self(stack):
    from astrbot_plugin_shadow_memory.memory import prompts

    assert "只有我说过、做过、感受到的事" in prompts.CLOSE_EVENT_SYSTEM
    assert "对方说过、做过的事，必须用转录里的名字或昵称" in prompts.CLOSE_EVENT_SYSTEM
    assert "共同经历用" in prompts.CLOSE_EVENT_SYSTEM


async def test_rebuild_event_updates_text_and_preserves_dynamics(stack):
    base = 1_700_000_000_000
    await _observe(stack, "我爸妈支持我喜欢的东西", base)
    stack.text.push({"fragment": "我爸妈支持我喜欢的东西", "entities": ["爸妈"]})
    await stack.segmenter.close_event(1)
    before = await stack.db.fetchone("SELECT * FROM events WHERE id=1")
    stack.text.push({"fragment": "小明说爸妈支持他喜欢的东西", "entities": ["小明", "爸妈"]})
    rebuilt = await stack.segmenter.rebuild_event(1, expected_bot_id="bot1")
    after = await stack.db.fetchone("SELECT * FROM events WHERE id=1")
    assert rebuilt["fragment"] == "小明说爸妈支持他喜欢的东西"
    assert after["fragment"] == "小明说爸妈支持他喜欢的东西"
    assert after["status"] == before["status"]
    assert after["salience"] == before["salience"]
    assert after["storage_strength"] == pytest.approx(before["storage_strength"])
    assert after["retrievability"] == pytest.approx(before["retrievability"])


async def test_rebuild_event_rejects_open_or_missing_turns(stack):
    await _observe(stack, "还没结束", 1_700_000_000_000)
    with pytest.raises(ValueError, match="只有已关闭"):
        await stack.segmenter.rebuild_event(1, expected_bot_id="bot1")
    await stack.db.execute(
        "INSERT INTO events(id, bot_id, umo, kind, started_at, fragment, status, created_at) "
        "VALUES(2,'bot1','umo1','episodic',1,'旧的','closed',1)"
    )
    with pytest.raises(ValueError, match="没有原始对话"):
        await stack.segmenter.rebuild_event(2, expected_bot_id="bot1")
    with pytest.raises(ValueError, match="不属于"):
        await stack.segmenter.rebuild_event(1, expected_bot_id="bot2")


async def test_empty_event_is_deleted(stack):
    await stack.db.execute(
        "INSERT INTO events(bot_id, umo, kind, started_at, status, created_at) VALUES(?,?,?,?,?,?)",
        ("bot1", "umo1", "episodic", 1, "open", 1),
    )
    assert await stack.segmenter.close_event(1) is None
    assert await stack.db.scalar("SELECT COUNT(*) FROM events") == 0


async def test_topic_end_with_question_is_not_closed(stack):
    base = 1_700_000_000_000
    await _observe(stack, "你觉得呢", base)
    closed = await stack.segmenter.maybe_close_after_turn(
        "bot1", "umo1", "那你呢？", 0.9
    )
    assert closed is False


async def test_topic_end_closes_when_reply_is_finished(stack):
    base = 1_700_000_000_000
    stack.text.push({"fragment": "那天聊到很晚", "entities": []})
    await _observe(stack, "晚安", base)
    closed = await stack.segmenter.maybe_close_after_turn("bot1", "umo1", "晚安。", 0.8)
    assert closed is True
    event = await stack.db.fetchone("SELECT * FROM events WHERE id=1")
    assert event["status"] == "closed"


async def test_topic_end_below_threshold_keeps_open(stack):
    base = 1_700_000_000_000
    await _observe(stack, "继续聊", base)
    assert await stack.segmenter.maybe_close_after_turn("bot1", "umo1", "嗯", 0.3) is False


async def test_close_idle_sweeps_stale_events(stack):
    base = 1_700_000_000_000
    stack.text.push({"fragment": "旧的经历", "entities": []})
    await _observe(stack, "很久以前的话", base)
    closed = await stack.segmenter.close_idle(base + 40 * 60_000)
    assert closed == 1
    event = await stack.db.fetchone("SELECT status FROM events WHERE id=1")
    assert event["status"] == "closed"


async def test_big_event_requires_summary(stack, make_configs):
    cfg = make_configs({"memory": {"big_event_chars": 10}})
    local = build_stack(stack.db, cfg)
    await _observe(local, "这句话确实超过了十个字", 1_700_000_000_000)
    local.text.push({"fragment": "一段很长的经历", "entities": [], "summary": ""})
    await local.segmenter.close_event(1)
    event = await stack.db.fetchone("SELECT * FROM events WHERE id=1")
    assert event["truncated"] == 1
    assert event["summary"]


async def test_participants_are_recorded(stack):
    base = 1_700_000_000_000
    await _observe(stack, "我来了", base, name="小明")
    await stack.service.observe(
        Turn("bot1", "umo1", "user", "我也在", base + 1000, "u2", "小红"),
        display_name="测试群",
        chat_type="group",
    )
    event = await stack.db.fetchone("SELECT participants FROM events WHERE id=1")
    assert "小明" in event["participants"] and "小红" in event["participants"]
