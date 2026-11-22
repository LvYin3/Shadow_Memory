"""一次唤醒的编排：主持人与玩家、裁定骰、补叙、原子提交、进入记忆。"""

from __future__ import annotations

import json

import pytest

from astrbot_plugin_shadow_memory.memory import prompts as memory_prompts
from astrbot_plugin_shadow_memory.story import prompts
from astrbot_plugin_shadow_memory.story.director import SceneFailed
from astrbot_plugin_shadow_memory.story.llm import StoryLLM, StoryLLMError
from astrbot_plugin_shadow_memory.story.store import story_umo

from .helpers import text_of_len
from .story_helpers import (
    ACTION,
    BASE_CARD,
    BOT,
    CLOSING,
    HOUR,
    MEMORY,
    MINUTE,
    NOW,
    OPENING,
    PLAIN_TURN,
    STORY_ON,
    ScriptedRng,
    build_story,
    script_scene,
    start_story,
    world_seed,
)


@pytest.fixture
def env(db, make_configs):
    return build_story(db, make_configs(STORY_ON))


async def story_events(db):
    return await db.fetchall("SELECT * FROM events WHERE source='story' ORDER BY id")


# --------------------------------------------------------------------- 完整的一场
async def test_scene_commits_memory_card_canon_notes_and_schedule(env):
    await start_story(env)
    script_scene(env)
    env.clock.advance(MINUTE)
    result = await env.director.run(BOT)

    events = await story_events(env.db)
    assert len(events) == 1
    event = events[0]
    # 与聊天事件同表同权：仍是 episodic、已关闭，只靠 source 区分
    assert (event["kind"], event["status"], event["source"]) == ("episodic", "closed", "story")
    assert event["umo"] == story_umo(BOT)
    assert event["fragment"] == MEMORY["fragment"]
    assert event["salience"] == 3
    assert event["storage_strength"] == pytest.approx(env.stack.dynamics.s0_event(3))
    assert json.loads(event["entities"]) == ["便利店", "伞", "雨"]

    turns = await env.db.fetchall(
        "SELECT role, speaker_name, text FROM event_turns WHERE event_id=? ORDER BY seq",
        (event["id"],),
    )
    assert [turn["role"] for turn in turns] == ["narrator", "assistant", "narrator"]
    assert turns[0]["speaker_name"] == "旁白"
    assert turns[1]["text"] == "（心里：其实不想回去）我走进便利店，想买把伞。"

    scene = await env.db.fetchone("SELECT * FROM story_scenes")
    assert (scene["status"], scene["cause"], scene["rounds"]) == ("ok", "scheduled", 1)
    assert scene["event_id"] == event["id"] and scene["backfill_event_id"] is None
    # 背景模型 3 次（开场、裁定、收场）+ 她 1 次 + 整理 1 次 + 显著度 1 次 + 常驻背景 1 次
    assert scene["calls"] == 7 == result.calls
    assert json.loads(scene["seed"])["focus"]

    canon = await env.store.canon(BOT)
    assert [(row["origin"], row["name"]) for row in canon] == [
        ("seed", "青屿大学"),
        ("seed", "阿澄"),
        ("play", "便利店老板"),
    ]
    assert canon[-1]["category"] == "person" and canon[-1]["scene_id"] == scene["id"]
    notes = await env.store.notes(BOT)
    assert [(note["title"], note["body"]) for note in notes] == [("暗线", "室友阿澄在瞒着她找工作")]

    state = await env.store.state(BOT)
    card = json.loads(state["card"])
    assert (card["where"], card["doing"], card["phone_ok"]) == ("便利店", "躲雨", True)
    assert card["until"] == env.clock.now + 40 * MINUTE
    assert state["next_wake_at"] == env.clock.now + 45 * MINUTE == result.next_wake_at
    assert state["last_scene_at"] == env.clock.now
    # 开启时带一天的额度（10），一分钟回补不到 0.01，演一场扣 1
    assert state["tokens"] == pytest.approx(9.0, abs=0.01)
    assert state["failures"] == 0

    # 实体建了边，高显著度的事进了常驻背景
    assert await env.db.scalar("SELECT COUNT(*) FROM event_entities WHERE event_id=?", (event["id"],)) == 3
    assert await env.db.scalar("SELECT COUNT(*) FROM ambient_memories WHERE source_event_id=?", (event["id"],)) == 1


async def test_first_scene_opens_from_the_seed_without_backfill(env):
    await start_story(env)
    script_scene(env)
    await env.director.run(BOT)
    opening_prompt = env.llm.prompts("gm")[0]
    assert "这是故事的第一场。" in opening_prompt
    assert "起点：她在合租屋，正在赖床。" in opening_prompt
    assert opening_prompt.endswith(prompts.GM_OPEN_TASK_FIRST)
    assert len(await story_events(env.db)) == 1


# --------------------------------------------------------------------- 权力边界
async def test_background_model_only_sees_observable_behaviour(env):
    """背景模型只读外显：人格三层、洞察、私人笔记和她的内心都不能出现在它的提示词里。"""
    persona_text = text_of_len(60, "我骨子里怕麻烦别人")
    await env.db.execute(
        "INSERT INTO persona_entries(bot_id, layer, text, source_event_ids, created_day, "
        "last_edited_day, active) VALUES(?,?,?,?,?,?,1)",
        (BOT, "core", persona_text, "[]", 1, 1),
    )
    await env.db.execute(
        "INSERT INTO insights(bot_id, text, source_event_ids, created_at) VALUES(?,?,?,?)",
        (BOT, "我发现自己总在逃避冲突", "[]", NOW),
    )
    await env.stack.notes.write(BOT, kind="diary", content="今天其实很想哭")
    await env.db.execute(
        "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, fragment, summary, "
        "salience, status, source, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (BOT, "umo1", "episodic", NOW - HOUR, NOW - HOUR, "小明说给我寄了一盒茶",
         None, 3, "closed", "chat", NOW - HOUR),
    )
    await start_story(env)
    script_scene(env)
    await env.director.run(BOT)

    gm_text = "\n".join(env.llm.prompts("gm") + env.llm.systems("gm"))
    for secret in (persona_text, "我发现自己总在逃避冲突", "今天其实很想哭", "其实不想回去", BASE_CARD):
        assert secret not in gm_text
    # 它能看到的：世界设定、聊天事件的碎片、她说出口做出来的事
    assert "青屿大学" in gm_text and "期末前要交一份小组报告" in gm_text
    assert "小明说给我寄了一盒茶" in gm_text
    assert "【她】我走进便利店，想买把伞。" in gm_text

    # 她自己带着完整的人设和人格进场景
    actor_system = env.llm.systems("actor")[0]
    assert BASE_CARD in actor_system and persona_text in actor_system
    # 世界设定和主线钩子是上帝视角，不直接给她
    assert "期末前要交一份小组报告" not in actor_system
    assert "一所靠海的综合性大学" not in actor_system


async def test_chat_digest_can_be_switched_off(db, make_configs):
    env = build_story(db, make_configs({"story": {"enabled": True, "chat_digest_items": 0}}))
    await db.execute(
        "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, fragment, salience, "
        "status, source, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (BOT, "umo1", "episodic", NOW - HOUR, NOW - HOUR, "小明说给我寄了一盒茶", 3,
         "closed", "chat", NOW - HOUR),
    )
    await start_story(env)
    script_scene(env)
    await env.director.run(BOT)
    assert "小明说给我寄了一盒茶" not in "\n".join(env.llm.prompts("gm"))


# --------------------------------------------------------------------- 裁定骰
CHECK_TURN = {
    "check": {
        "what": "讲价买下最后一把伞",
        "likelihood": "很可能",
        "success": "老板点了头，伞归你了。",
        "failure": "老板摇头，伞被后面的人买走了。",
    },
    "narration": "",
    "lookup": [],
    "end": True,
}


@pytest.mark.parametrize(
    ("dice", "narration", "label"),
    [
        ([0.50, 0.99], "老板点了头，伞归你了。", "成了"),
        ([0.90, 0.99], "老板摇头，伞被后面的人买走了。", "没成"),
    ],
)
async def test_dice_decide_uncertain_outcomes(db, make_configs, dice, narration, label):
    env = build_story(db, make_configs(STORY_ON), rng=ScriptedRng(dice))
    await start_story(env)
    script_scene(env, turns=[CHECK_TURN])
    await env.director.run(BOT)
    scene = await db.fetchone("SELECT rolls, transcript FROM story_scenes")
    transcript = json.loads(scene["transcript"])
    assert transcript[-1] == {"role": "narrator", "text": narration}
    rolls = json.loads(scene["rolls"])
    assert len(rolls) == 1
    assert (rolls[0]["likelihood"], rolls[0]["label"], rolls[0]["round"]) == ("很可能", label, 1)
    assert rolls[0]["what"] == "讲价买下最后一把伞"


async def test_twist_is_handed_to_the_next_background_call(db, make_configs):
    env = build_story(db, make_configs(STORY_ON), rng=ScriptedRng([0.90, 0.01]))
    await start_story(env)
    script_scene(env, turns=[CHECK_TURN])
    await env.director.run(BOT)
    closing_prompt = env.llm.prompts("gm")[-1]
    assert "结果是“没成，但有转机”，转机还没有体现" in closing_prompt


async def test_check_without_both_branches_is_rejected(env):
    await start_story(env)
    broken = {"check": {"likelihood": "很可能", "success": "成了。", "failure": ""}, "end": True}
    script_scene(env, turns=[broken])
    with pytest.raises(SceneFailed):
        await env.director.run(BOT)
    assert await story_events(env.db) == []


# --------------------------------------------------------------------- 轮数
async def test_round_cap_forces_the_scene_to_close(db, make_configs):
    env = build_story(db, make_configs({"story": {"enabled": True, "max_rounds": 2}}))
    await start_story(env)
    never_ends = {**PLAIN_TURN, "end": False}
    script_scene(env, turns=[never_ends, never_ends])
    result = await env.director.run(BOT)
    assert result.rounds == 2
    assert len(env.llm.prompts("actor")) == 2
    gm_prompts = env.llm.prompts("gm")
    assert prompts.GM_TURN_LAST_HINT not in gm_prompts[1]
    assert prompts.GM_TURN_LAST_HINT in gm_prompts[2]
    assert env.llm.queues == {"gm": [], "actor": []}


async def test_scene_can_end_early(env):
    await start_story(env)
    script_scene(env, turns=[{**PLAIN_TURN, "end": False}, PLAIN_TURN])
    result = await env.director.run(BOT)
    assert result.rounds == 2  # 上限是 4，背景模型第二轮就收了


# --------------------------------------------------------------------- 补叙
async def test_gap_is_backfilled_once_with_lowest_salience(env):
    await start_story(env)
    script_scene(env)
    await env.director.run(BOT)
    first_end = env.clock.now

    env.clock.advance(3 * HOUR + 20 * MINUTE)
    script_scene(
        env,
        opening={**OPENING, "backfill": "你在便利店等雨停，后来撑着伞走回了合租屋。"},
        memory={**MEMORY, "fragment": "我回到合租屋", "backfill_fragment": "我等雨停了才撑伞走回去"},
    )
    result = await env.director.run(BOT)

    opening_prompt = env.llm.prompts("gm")[3]
    assert "距上一场结束：3 小时 20 分钟。" in opening_prompt
    assert opening_prompt.endswith(prompts.GM_OPEN_TASK_BACKFILL)
    # 她在场景里先读到补叙
    assert "【旁白】你在便利店等雨停，后来撑着伞走回了合租屋。" in env.llm.prompts("actor")[1]

    backfill = await env.db.fetchone("SELECT * FROM events WHERE id=?", (result.backfill_event_id,))
    assert (backfill["source"], backfill["salience"], backfill["status"]) == ("story", 1, "closed")
    assert backfill["fragment"] == "我等雨停了才撑伞走回去"
    assert (backfill["started_at"], backfill["ended_at"]) == (first_end, env.clock.now)
    assert len(await story_events(env.db)) == 3


async def test_short_gap_needs_no_backfill(env):
    await start_story(env)
    script_scene(env)
    await env.director.run(BOT)
    env.clock.advance(12 * MINUTE)
    script_scene(env, opening={**OPENING, "backfill": "背景模型多写的补叙"})
    result = await env.director.run(BOT)
    assert env.llm.prompts("gm")[3].endswith(prompts.GM_OPEN_TASK_PLAIN)
    assert result.backfill_event_id is None
    assert "背景模型多写的补叙" not in env.llm.prompts("actor")[1]
    assert len(await story_events(env.db)) == 2


# --------------------------------------------------------------------- 预约与状态卡
@pytest.mark.parametrize(
    ("asked", "minutes"), [(1, 10), (45, 45), (99999, 720), (None, 720), ("abc", 720)]
)
async def test_next_wake_is_clamped(env, asked, minutes):
    await start_story(env)
    script_scene(env, closing={**CLOSING, "next_wake_minutes": asked})
    result = await env.director.run(BOT)
    assert result.next_wake_at == env.clock.now + minutes * MINUTE


async def test_closing_without_a_card_fails_the_scene(env):
    await start_story(env)
    script_scene(env, closing={**CLOSING, "card": {}})
    with pytest.raises(SceneFailed):
        await env.director.run(BOT)
    assert await story_events(env.db) == []
    assert (await env.store.state(BOT))["card"] is None


async def test_canon_is_append_only_and_notes_can_be_rewritten(env):
    await start_story(env)
    script_scene(env)
    await env.director.run(BOT)
    env.clock.advance(15 * MINUTE)
    script_scene(
        env,
        closing={
            **CLOSING,
            "canon": [
                {"name": "便利店老板", "category": "人物", "body": "姓周，话少。"},
                {"name": "青屿大学", "category": "地点", "body": "其实在山里。"},
            ],
            "notes": [{"title": "暗线", "body": ""}, {"title": "伏笔", "body": "那把伞是别人落下的"}],
        },
    )
    await env.director.run(BOT)
    canon = await env.store.canon(BOT)
    assert [(row["origin"], row["name"], row["body"]) for row in canon] == [
        ("seed", "青屿大学", "一所靠海的综合性大学。"),  # 种子条目不会被改写
        ("seed", "阿澄", "她的室友，学美术，作息混乱。"),
        ("play", "便利店老板", "姓周，话少。"),  # 重复登记不会多出一条
        ("play", "青屿大学", "其实在山里。"),
    ]
    assert [note["title"] for note in await env.store.notes(BOT)] == ["伏笔"]


async def test_notes_and_scene_summaries_reach_the_next_scene(env):
    await start_story(env)
    script_scene(env)
    await env.director.run(BOT)
    env.clock.advance(15 * MINUTE)
    script_scene(env)
    await env.director.run(BOT)
    opening_prompt = env.llm.prompts("gm")[3]
    assert "## 暗线\n室友阿澄在瞒着她找工作" in opening_prompt
    assert "她在便利店躲雨，买到最后一把伞。" in opening_prompt
    assert "[人物] 便利店老板：姓周，话少。" in opening_prompt
    assert "地点：便利店；在做：躲雨" in opening_prompt


async def test_lookup_results_come_back_on_the_next_call(env):
    await start_story(env)
    script_scene(env, opening={**OPENING, "lookup": ["阿澄"]})
    await env.director.run(BOT)
    assert "# 你查的设定\n- [人物] 阿澄：她的室友，学美术，作息混乱。" in env.llm.prompts("gm")[1]


# --------------------------------------------------------------------- 失败与原子性
async def test_failed_scene_leaves_nothing_behind(env):
    await start_story(env)
    before = await env.store.state(BOT)
    env.llm.push("gm", dict(OPENING))
    env.llm.push("actor", {"inner": "", "action": ""})  # 她没给出行动
    with pytest.raises(SceneFailed) as error:
        await env.director.run(BOT)
    assert error.value.progress.transcript == [{"role": "narrator", "text": OPENING["narration"]}]
    assert await story_events(env.db) == []
    after = await env.store.state(BOT)
    for key in ("tokens", "tokens_at", "card", "next_wake_at", "last_scene_at"):
        assert after[key] == before[key]


async def test_commit_is_atomic(env, monkeypatch):
    """提交到一半出错：事件、场景记录、设定都不能留下半截。"""
    await start_story(env)
    script_scene(env)

    async def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(env.store, "apply_notes", boom)
    with pytest.raises(RuntimeError):
        await env.director.run(BOT)
    assert await env.db.scalar("SELECT COUNT(*) FROM events") == 0
    assert await env.db.scalar("SELECT COUNT(*) FROM event_turns") == 0
    assert await env.db.scalar("SELECT COUNT(*) FROM story_scenes") == 0
    assert await env.db.scalar("SELECT COUNT(*) FROM story_canon WHERE origin='play'") == 0
    assert (await env.store.state(BOT))["card"] is None


async def test_memory_writer_failure_falls_back_to_her_last_action(env):
    await start_story(env)
    script_scene(env, memory=None)  # 写作模型没有输出
    result = await env.director.run(BOT)
    assert result.fragment == ACTION["action"]
    event = (await story_events(env.db))[0]
    assert event["fragment"] == ACTION["action"] and json.loads(event["entities"]) == []


async def test_requires_seed_and_base_card(db, make_configs):
    env = build_story(db, make_configs(STORY_ON))
    with pytest.raises(SceneFailed, match="世界种子"):
        await env.director.run(BOT)
    await env.service.import_seed(BOT, world_seed())
    env.base["card"] = ("", "")
    with pytest.raises(SceneFailed, match="基础人设"):
        await env.director.run(BOT)
    assert env.llm.calls == []


# --------------------------------------------------------------------- 与记忆系统同权
async def test_story_event_is_recalled_and_shown_in_ambient_like_chat(env):
    await start_story(env)
    script_scene(env)
    result = await env.director.run(BOT)
    later = env.clock.now + HOUR
    candidates, _ = await env.stack.recall.candidates(BOT, "那把伞是在便利店买的吗", later)
    assert result.event_id in {candidate.event_id for candidate in candidates}
    snapshot = await env.stack.ambient.snapshot(BOT, later)
    assert result.event_id in snapshot.important_event_ids + snapshot.recent_event_ids


async def test_rebuilding_a_story_event_uses_the_story_prompt(env):
    await start_story(env)
    script_scene(env)
    result = await env.director.run(BOT)
    env.stack.text.push({"fragment": "我买了把黑伞", "entities": ["伞"], "summary": ""})
    rebuilt = await env.stack.service.rebuild_event(BOT, result.event_id)
    assert rebuilt["fragment"] == "我买了把黑伞"
    system, user = env.stack.text.calls[-1]
    assert system == memory_prompts.STORY_EVENT_SYSTEM[:20]
    assert user.startswith("对话（3 轮")


async def test_segmenter_ignores_story_events(env):
    """故事事件直接以已关闭状态写入，空闲巡检和话题分段都不该碰它。"""
    await start_story(env)
    script_scene(env)
    await env.director.run(BOT)
    assert await env.stack.service.close_idle(env.clock.now + 10 * HOUR) == 0
    assert await env.db.scalar("SELECT COUNT(*) FROM events WHERE status='open'") == 0


# --------------------------------------------------------------------- 真实的模型封装
class FakeProvider:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests = []

    async def text_chat(self, **kwargs):
        self.requests.append(kwargs)
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return type("Response", (), {"completion_text": value})()


def make_llm(provider, audit):
    async def resolver(role, bot_id):
        return provider

    return StoryLLM(resolver, audit)


async def test_llm_retries_once_on_bad_output(env):
    provider = FakeProvider(["不是 JSON", '```json\n{"narration": "好"}\n```'])
    value, attempts = await make_llm(provider, env.stack.audit).ask_json("gm", BOT, "系统", "用户")
    assert value == {"narration": "好"} and attempts == 2
    assert provider.requests[0] == {"prompt": "用户", "system_prompt": "系统", "contexts": []}


async def test_llm_gives_up_after_retry(env):
    provider = FakeProvider([RuntimeError("timeout"), '{"narration": ""}'])
    llm = make_llm(provider, env.stack.audit)
    with pytest.raises(StoryLLMError) as error:
        await llm.ask_json("gm", BOT, "s", "u", validate=lambda v: bool(v.get("narration")))
    assert error.value.attempts == 2
    assert llm.stats == {"calls": 2, "failures": 1}


async def test_llm_without_provider(env):
    llm = make_llm(None, env.stack.audit)
    assert await llm.available("gm", BOT) is False
    with pytest.raises(StoryLLMError, match="背景模型"):
        await llm.ask_json("gm", BOT, "s", "u")
