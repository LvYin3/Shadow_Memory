"""故事模块门面：启停前置条件、到期巡检、额度、失败退避、状态卡注入。"""

from __future__ import annotations

import pytest

from astrbot_plugin_shadow_memory.story.card import CHAT_BOUNDARY_LINE
from astrbot_plugin_shadow_memory.story.service import StoryError
from astrbot_plugin_shadow_memory.story.world import WorldSeedError

from .story_helpers import (
    BOT,
    CLOSING,
    DAY,
    HOUR,
    MINUTE,
    OPENING,
    STORY_ON,
    build_story,
    script_scene,
    start_story,
    world_seed,
)


@pytest.fixture
def env(db, make_configs):
    return build_story(db, make_configs(STORY_ON))


async def story_event_count(db) -> int:
    return await db.scalar("SELECT COUNT(*) FROM events WHERE source='story'")


# --------------------------------------------------------------------- 开启的前置条件
async def test_enable_reports_everything_that_is_missing(db, make_configs):
    env = build_story(db, make_configs({}), base_card=("", ""))
    env.llm.unavailable = {"gm", "actor"}
    with pytest.raises(StoryError) as error:
        await env.service.enable(BOT)
    message = str(error.value)
    for needle in ("总开关", "世界种子", "基础人设", "背景模型", "场景中的她"):
        assert needle in message
    state = await env.store.state(BOT)
    assert state is None or not state["enabled"]


async def test_no_seed_no_story(env):
    """没有世界种子就不启动，不做自动生成兜底。"""
    with pytest.raises(StoryError, match="世界种子"):
        await env.service.enable(BOT)
    assert await env.service.tick() == []
    assert env.llm.calls == []


async def test_bad_seed_is_rejected_and_changes_nothing(env):
    with pytest.raises(WorldSeedError):
        await env.service.import_seed(BOT, {"version": "world_seed/v1", "outline": "太短"})
    assert await env.store.state(BOT) is None


async def test_enable_starts_with_one_day_of_budget_and_wakes_soon(env):
    await start_story(env)
    status = await env.service.status(BOT)
    assert status["enabled"] and not status["paused"] and status["has_seed"]
    assert status["tokens"] == 10 and status["capacity"] == 30
    assert status["next_wake_at"] == env.clock.now
    assert status["world_title"] == "青屿"
    assert status["canon"] == {"seed": 2, "play": 0}
    assert status["base_card_source"] == "story.base_card"
    assert status["problems"] == []


async def test_toggling_off_and_on_does_not_mint_budget(env):
    await start_story(env)
    await env.store.update_state(BOT, tokens=0.5, tokens_at=env.clock.now)
    await env.service.disable(BOT)
    env.clock.advance(10 * DAY)
    status = await env.service.enable(BOT)
    assert status["tokens"] == 10  # 关着的十天不算，最多带一天的量
    await env.store.update_state(BOT, tokens=0.5, tokens_at=env.clock.now)
    await env.service.disable(BOT)
    status = await env.service.enable(BOT)
    assert status["tokens"] == 0.5  # 立刻重开也不会凭空变多


async def test_seed_can_be_reimported_without_losing_play_canon(env):
    await start_story(env)
    script_scene(env)
    await env.service.tick()
    seed = world_seed()
    seed["title"] = "青屿（修订）"
    seed["entries"] = [{"category": "地点", "name": "北门夜市", "body": "晚上十点收摊。"}]
    result = await env.service.import_seed(BOT, seed)
    assert result == {"title": "青屿（修订）", "entries": 1, "hooks": 1}
    canon = await env.store.canon(BOT)
    assert [(row["origin"], row["name"]) for row in canon] == [
        ("seed", "北门夜市"),
        ("play", "便利店老板"),
    ]
    state = await env.store.state(BOT)
    assert state["enabled"] == 1 and state["card"]  # 运行状态不受影响


# --------------------------------------------------------------------- 巡检与额度
async def test_tick_plays_only_when_due(env):
    await start_story(env)
    script_scene(env)
    assert await env.service.tick() == [BOT]
    assert await story_event_count(env.db) == 1
    # 预约在 45 分钟后：没到点不演
    env.clock.advance(44 * MINUTE)
    assert await env.service.tick() == []
    assert len(env.llm.calls) == 4
    env.clock.advance(MINUTE)
    script_scene(env)
    assert await env.service.tick() == [BOT]
    assert await story_event_count(env.db) == 2


async def test_master_switch_off_stops_everything(db, make_configs):
    on = build_story(db, make_configs(STORY_ON))
    await start_story(on)
    off = build_story(db, make_configs({}), clock=on.clock)
    script_scene(off)
    assert await off.service.tick() == []
    assert off.llm.calls == []
    assert await off.service.chat_lines(BOT) == []


async def test_empty_bucket_is_silent_and_waits_for_refill(env):
    """额度耗尽不报错：不调用模型，不记失败，等攒够一次再来。"""
    await start_story(env)
    await env.store.update_state(BOT, tokens=0.2, tokens_at=env.clock.now)
    script_scene(env)
    assert await env.service.tick() == []
    assert env.llm.calls == []
    state = await env.store.state(BOT)
    assert state["failures"] == 0 and state["paused"] == 0
    # 每天回补 10 次，还差 0.8 次 → 115.2 分钟
    assert state["next_wake_at"] == env.clock.now + int(0.8 * DAY / 10)
    assert await env.db.scalar("SELECT COUNT(*) FROM story_scenes") == 0

    env.clock.advance(int(0.8 * DAY / 10))
    assert await env.service.tick() == [BOT]
    assert (await env.store.state(BOT))["tokens"] == pytest.approx(0.0, abs=1e-6)


async def test_unused_budget_carries_over_but_is_capped(env):
    await start_story(env)
    env.clock.advance(HOUR)
    assert (await env.service.status(BOT))["tokens"] == pytest.approx(10 + 10 / 24, abs=0.01)
    env.clock.advance(30 * DAY)
    assert (await env.service.status(BOT))["tokens"] == 30


async def test_downtime_just_becomes_a_longer_gap(env):
    """宕机几天后恢复：只演一场（开场补叙），不逐个补演错过的唤醒点。"""
    await start_story(env)
    script_scene(env)
    await env.service.tick()
    env.clock.advance(3 * DAY)
    script_scene(env, opening={**OPENING, "backfill": "这三天你照常上课、回合租屋。"})
    assert await env.service.tick() == [BOT]
    assert "距上一场结束：3 天。" in env.llm.prompts("gm")[3]
    assert await env.service.tick() == []
    assert await env.db.scalar("SELECT COUNT(*) FROM story_scenes WHERE status='ok'") == 2


# --------------------------------------------------------------------- 失败与暂停
async def test_failed_scene_keeps_the_token_and_retries_later(env):
    await start_story(env)
    env.llm.push("gm", dict(OPENING))  # 之后她没有输出 → 这一场失败
    assert await env.service.tick() == []
    assert await story_event_count(env.db) == 0
    state = await env.store.state(BOT)
    assert state["tokens"] == 10  # 没扣额度
    assert state["failures"] == 1 and state["paused"] == 0
    assert state["next_wake_at"] == env.clock.now + 30 * MINUTE
    assert state["card"] is None
    failed = await env.db.fetchone("SELECT status, error, transcript FROM story_scenes")
    assert failed["status"] == "failed" and "actor" in failed["error"]
    assert OPENING["narration"] in failed["transcript"]  # 留着排障

    env.clock.advance(30 * MINUTE)
    script_scene(env)
    assert await env.service.tick() == [BOT]
    assert (await env.store.state(BOT))["failures"] == 0


async def test_three_failures_in_a_row_pause_the_story(env):
    await start_story(env)
    for expected in (1, 2):
        assert await env.service.tick() == []
        assert (await env.store.state(BOT))["failures"] == expected
        env.clock.advance(30 * MINUTE)
    assert await env.service.tick() == []
    status = await env.service.status(BOT)
    assert status["paused"] and "连续 3 次失败" in status["pause_reason"]

    env.clock.advance(DAY)
    script_scene(env)
    assert await env.service.tick() == []  # 暂停后不再自动唤醒
    assert len(env.llm.calls) == 3

    resumed = await env.service.resume(BOT)
    assert not resumed["paused"] and resumed["failures"] == 0
    assert await env.service.tick() == [BOT]


async def test_unexpected_error_is_treated_as_a_failure(env, monkeypatch):
    await start_story(env)
    script_scene(env)

    async def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(env.store, "add_canon", boom)
    assert await env.service.tick() == []
    state = await env.store.state(BOT)
    assert state["failures"] == 1
    assert state["next_wake_at"] == env.clock.now + 30 * MINUTE
    assert await story_event_count(env.db) == 0
    failed = await env.db.fetchone("SELECT error FROM story_scenes WHERE status='failed'")
    assert "disk full" in failed["error"]


async def test_manual_pause_and_resume(env):
    await start_story(env)
    script_scene(env)
    paused = await env.service.pause(BOT)
    assert paused["paused"] and paused["pause_reason"] == "手动暂停"
    assert await env.service.tick() == []
    await env.service.resume(BOT)
    assert await env.service.tick() == [BOT]


# --------------------------------------------------------------------- 手动唤醒
async def test_manual_wake_is_free_and_ignores_the_schedule(env):
    await start_story(env)
    script_scene(env)
    await env.service.tick()
    tokens = (await env.store.state(BOT))["tokens"]
    env.clock.advance(MINUTE)  # 离预约还有 44 分钟，也没到最小间隔
    script_scene(env)
    result = await env.service.wake(BOT)
    assert result.tokens == pytest.approx(tokens, abs=0.01)
    scene = await env.db.fetchone("SELECT cause FROM story_scenes ORDER BY id DESC LIMIT 1")
    assert scene["cause"] == "manual"


async def test_manual_wake_failure_does_not_count_towards_auto_pause(env):
    await start_story(env)
    with pytest.raises(StoryError, match="没演成"):
        await env.service.wake(BOT)
    state = await env.store.state(BOT)
    assert state["failures"] == 0 and state["paused"] == 0
    assert await env.db.scalar("SELECT COUNT(*) FROM story_scenes WHERE status='failed'") == 1


async def test_manual_wake_needs_the_story_to_be_on(env):
    await env.service.import_seed(BOT, world_seed())
    with pytest.raises(StoryError, match="还没开启"):
        await env.service.wake(BOT)


async def test_only_one_scene_per_bot_at_a_time(env):
    await start_story(env)
    script_scene(env)
    async with env.service._lock(BOT):  # noqa: SLF001 - 模拟正在演一场
        assert env.service.is_running(BOT)
        with pytest.raises(StoryError, match="正在演"):
            await env.service.wake(BOT)
        assert await env.service.tick() == []
    assert env.llm.calls == []


# --------------------------------------------------------------------- 聊天侧
async def test_chat_lines_carry_the_card_and_the_boundary(env):
    await start_story(env)
    assert await env.service.chat_lines(BOT) == [CHAT_BOUNDARY_LINE]  # 还没演过，没有状态卡
    script_scene(env, closing={**CLOSING, "card": {**CLOSING["card"], "phone": "不方便"}})
    await env.service.tick()
    lines = await env.service.chat_lines(BOT)
    assert lines == [
        "- 我现在在便利店，正在躲雨，大概到14:40。",
        "- 这会儿不太方便看手机。",
        CHAT_BOUNDARY_LINE,
    ]
    # 过了预计结束时间，“不方便”自动失效
    env.clock.advance(2 * HOUR)
    lines = await env.service.chat_lines(BOT)
    assert len(lines) == 2 and lines[0].startswith("- 我之前在便利店")
    # 陈旧太久，整张卡不再注入
    env.clock.advance(2 * DAY)
    assert await env.service.chat_lines(BOT) == [CHAT_BOUNDARY_LINE]


async def test_chat_lines_are_empty_when_the_story_is_off(env):
    await env.service.import_seed(BOT, world_seed())
    assert await env.service.chat_lines(BOT) == []
    assert await env.service.chat_lines("unknown-bot") == []


async def test_chat_boundary_can_be_disabled(db, make_configs):
    env = build_story(db, make_configs({"story": {"enabled": True, "chat_boundary": False}}))
    await start_story(env)
    assert await env.service.chat_lines(BOT) == []


# --------------------------------------------------------------------- 观测与导出
async def test_status_shows_her_view_not_the_spoilers(env):
    await start_story(env)
    script_scene(env)
    await env.service.tick()
    status = await env.service.status(BOT)
    assert status["scenes_total"] == 1 and status["notes"] == 0
    assert status["canon"] == {"seed": 2, "play": 1}
    assert status["card_lines"] == ["- 我现在在便利店，正在躲雨，大概到14:40。"]
    scene = status["scenes"][0]
    assert scene["fragment"] == "我在便利店躲雨，买到了最后一把黑伞"
    assert scene["status"] == "ok" and scene["calls"] == 7
    # 运维页不带背景模型的梗概、笔记和完整转录
    flat = str(status)
    assert "室友阿澄在瞒着她找工作" not in flat
    assert CLOSING["summary"] not in flat
    assert "其实不想回去" not in flat


async def test_export_is_complete(env):
    await start_story(env)
    script_scene(env)
    await env.service.tick()
    data = await env.service.export(BOT)
    assert data["version"] == "shadow_story/export/v1" and data["bot_id"] == BOT
    assert data["state"]["card"]["where"] == "便利店"
    assert data["state"]["seed_meta"]["hooks"] == ["期末前要交一份小组报告"]
    assert [row["name"] for row in data["canon"]] == ["青屿大学", "阿澄", "便利店老板"]
    assert data["notes"] == []
    assert "暗线" in data["state"]["tree"]
    scene = data["scenes"][0]
    assert scene["summary"] == CLOSING["summary"]
    assert scene["transcript"][1]["inner"] == "其实不想回去"
    assert scene["seed"]["words"]


async def test_bots_are_independent(env):
    await start_story(env, "bot1")
    await start_story(env, "bot2")
    await env.service.disable("bot2")
    script_scene(env)
    assert await env.service.tick() == ["bot1"]
    assert (await env.service.status("bot2"))["scenes_total"] == 0
    assert await env.db.scalar("SELECT COUNT(*) FROM events WHERE bot_id='bot2'") == 0


async def test_platform_offline_defers_tick_and_blocks_manual_wake(env, caplog):
    import logging

    issue = {"text": "aiocqhttp 反向 WebSocket 尚未连接"}
    env.service._platform_issue = lambda bot_id: issue["text"]

    # 平台尚未连上时仍允许导入种子和开启故事，状态里透出 platform_warning
    await env.service.import_seed(BOT, world_seed())
    enabled = await env.service.enable(BOT)
    assert enabled["enabled"] is True
    assert enabled["problems"] == []
    assert enabled["platform_warning"] == issue["text"]
    tokens_before = enabled["tokens"]

    # 定时巡检暂缓唤醒：不跑场、不扣额度、不记失败，连续巡检不重复刷等待日志
    with caplog.at_level(logging.INFO, logger="pm-test"):
        assert await env.service.tick() == []
        assert await env.service.tick() == []
    state = await env.store.state(BOT)
    assert int(state["failures"] or 0) == 0
    assert (await env.service.status(BOT))["tokens"] == tokens_before
    assert (await env.service.status(BOT))["scenes_total"] == 0
    wait_logs = [
        record.getMessage()
        for record in caplog.records
        if "story_wait_platform" in record.getMessage()
    ]
    assert len(wait_logs) == 1

    # 手动立即演一场直接告知平台未连接
    with pytest.raises(StoryError, match="反向 WebSocket 尚未连接"):
        await env.service.wake(BOT)

    # 平台恢复连接后，下一轮巡检正常唤醒
    issue["text"] = ""
    script_scene(env)
    assert await env.service.tick() == [BOT]
    after = await env.service.status(BOT)
    assert after["platform_warning"] == ""
    assert after["scenes_total"] == 1
