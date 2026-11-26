"""一次唤醒的编排：主持人与玩家、裁定骰、补叙、原子提交、进入记忆。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_shadow_memory.memory import prompts as memory_prompts
from astrbot_plugin_shadow_memory.story import prompts
from astrbot_plugin_shadow_memory.story.cast import CastTools
from astrbot_plugin_shadow_memory.story.director import SceneFailed, SceneProgress
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


def with_name_guard(db, make_configs):
    """真名拦截默认关；要测拦截本身时显式打开。"""
    return build_story(db, make_configs({"story": {"enabled": True, "guard_real_names": True}}))


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
    assert await env.store.notes(BOT) == []
    stored = json.loads((await env.store.state(BOT))["tree"])
    assert stored["threads"][0]["title"] == "暗线"
    assert stored["threads"][0]["people"] == ["阿澄"]
    assert stored["threads"][0]["scenes"] == [scene["id"]]

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


@pytest.mark.parametrize(("style", "tag"), [("first", "【眼前】"), ("none", "【眼前】")])
async def test_narration_style_is_configurable(db, make_configs, style, tag):
    env = build_story(db, make_configs({"story": {"enabled": True, "narration": style}}))
    await start_story(env)
    script_scene(env)
    await env.director.run(BOT)
    env.clock.advance(3 * HOUR)
    script_scene(env, opening={**OPENING, "backfill": "之前等雨停。"})
    await env.director.run(BOT)

    assert all(system == prompts.gm_system(style) for system in env.llm.systems("gm"))
    gm_prompts = env.llm.prompts("gm")
    assert f"{tag}店里只剩一把黑伞" in gm_prompts[2]  # 收场时看到的经过
    assert f"【她】我走进便利店，想买把伞。\n{tag}店里只剩一把黑伞" in gm_prompts[3]  # 上一场的结尾
    assert prompts.narration(style).hint in gm_prompts[3]  # 补叙的人称提示
    assert "【旁白】" not in "\n".join(gm_prompts + env.llm.prompts("actor"))
    assert f"{tag}之前等雨停。" in env.llm.prompts("actor")[1]
    assert "其实不想回去" not in "\n".join(gm_prompts)
    # 落库的转录与人称无关
    turns = await env.db.fetchall("SELECT DISTINCT role, speaker_name FROM event_turns ORDER BY role")
    assert {(turn["role"], turn["speaker_name"]) for turn in turns} == {
        ("assistant", ""),
        ("narrator", "旁白"),
    }


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


# --------------------------------------------------------------------- 不替现实里的人说话
async def chat_with(db, name: str = "小明", fragment: str = "小明说给我寄了一盒茶") -> None:
    """她在聊天里认识的一个真人，以及一条相关的聊天记忆。"""
    await db.execute(
        "INSERT INTO people(bot_id, person_id, name, aliases, last_topic, last_seen, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (BOT, "u1", name, '["明明"]', None, NOW, NOW, NOW),
    )
    await db.execute(
        "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, fragment, salience, "
        "status, source, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (BOT, "umo1", "episodic", NOW - HOUR, NOW - HOUR, fragment, 3, "closed", "chat",
         NOW - HOUR),
    )


FAKE_MESSAGE = "你的手机弹出一条消息，是小明：「睡了没」。"


@pytest.mark.parametrize(
    "script",
    [
        {"opening": {**OPENING, "narration": FAKE_MESSAGE}},
        {"turns": [{**PLAIN_TURN, "narration": FAKE_MESSAGE}]},
        {
            "turns": [
                {
                    "check": {
                        "what": "等回信",
                        "likelihood": "很可能",
                        "success": "小明回了：「我这边下雪了。」",
                        "failure": "手机一直没亮。",
                    },
                    "end": True,
                }
            ]
        },
        {"closing": {**CLOSING, "epilogue": "临睡前小明又发来一句晚安。"}},
    ],
    ids=["opening", "narration", "check-branch", "epilogue"],
)
async def test_background_model_cannot_speak_for_real_people(db, make_configs, script):
    """背景模型写“某某发来消息”会让她记住一段现实里没发生过的对话，必须拦下。"""
    env = with_name_guard(db, make_configs)
    await chat_with(env.db)
    await start_story(env)
    script_scene(env, **script)
    with pytest.raises(SceneFailed, match="现实里的人"):
        await env.director.run(BOT)
    assert await story_events(env.db) == []


async def test_real_names_pass_when_the_guard_is_off(env):
    """默认不拦真名：叙述里出现小明也能演完。"""
    await chat_with(env.db)
    await start_story(env)
    script_scene(env, opening={**OPENING, "narration": FAKE_MESSAGE})
    result = await env.director.run(BOT)
    assert result.rounds == 1


async def test_real_people_may_appear_in_private_notes_but_not_in_canon(db, make_configs):
    env = with_name_guard(db, make_configs)
    await chat_with(env.db)
    await start_story(env)
    script_scene(
        env,
        closing={
            **CLOSING,
            "canon": [
                {"name": "小明", "category": "人物", "body": "她的网友，住在北方。"},
                {"name": "茶", "category": "物品", "body": "小明寄来的一盒茶。"},
                {"name": "便利店老板", "category": "人物", "body": "姓周，话少。"},
            ],
            "notes": [{"title": "寄来的茶", "body": "小明说寄了茶，还没到，也没给单号。"}],
            "tree": {
                "threads": [
                    {"title": "寄来的茶", "people": ["小明"], "canon": [], "seed": [], "scenes": []}
                ]
            },
        },
    )
    await env.director.run(BOT)
    assert [row["name"] for row in await env.store.canon(BOT) if row["origin"] == "play"] == [
        "便利店老板"
    ]
    assert await env.store.notes(BOT) == []
    stored = json.loads((await env.store.state(BOT))["tree"])
    assert stored["threads"][0]["people"] == []


async def test_aliases_are_guarded_too(db, make_configs):
    env = with_name_guard(db, make_configs)
    await chat_with(env.db, fragment="明明说给我寄了一盒茶")
    await start_story(env)
    script_scene(env, opening={**OPENING, "narration": "明明发来一张雪景照片。"})
    with pytest.raises(SceneFailed, match="「明明」"):
        await env.director.run(BOT)


async def test_names_the_background_model_was_never_shown_are_not_guarded(db, make_configs):
    """聊天摘要关掉后它根本不知道这些名字；世界里碰巧有同名的人不该被误伤。"""
    env = build_story(db, make_configs({"story": {"enabled": True, "chat_digest_items": 0}}))
    await chat_with(db)
    await start_story(env)
    script_scene(env, opening={**OPENING, "narration": "楼下面馆的伙计小明冲你点了点头。"})
    result = await env.director.run(BOT)
    assert result.rounds == 1


async def test_world_seed_wins_when_a_contact_shares_a_name_with_a_character(env):
    await chat_with(env.db, name="阿澄", fragment="阿澄说周末想去海边")
    await start_story(env)
    script_scene(env, opening={**OPENING, "narration": "阿澄从房间里探出头，问你吃不吃夜宵。"})
    result = await env.director.run(BOT)
    assert result.rounds == 1


SPOKEN = "店员问要不要袋子。我说：“不用了，谢谢。”然后把伞夹在胳膊底下。"


async def test_first_person_narration_cannot_speak_for_her(db, make_configs):
    env = build_story(db, make_configs({"story": {"enabled": True, "narration": "first"}}))
    await start_story(env)
    script_scene(env, turns=[{**PLAIN_TURN, "narration": SPOKEN}])
    with pytest.raises(SceneFailed, match="替她说了话"):
        await env.director.run(BOT)
    assert await story_events(env.db) == []


@pytest.mark.parametrize(
    "text",
    [
        "店员问我要不要袋子，说完把伞递过来。",  # 别人问她，不是她开口
        "我听见有人说：“雨要下到明天。”",
        "我手里的伞还在滴水。",
    ],
)
async def test_first_person_perception_is_not_mistaken_for_speech(db, make_configs, text):
    env = build_story(db, make_configs({"story": {"enabled": True, "narration": "first"}}))
    await start_story(env)
    script_scene(env, turns=[{**PLAIN_TURN, "narration": text}])
    assert (await env.director.run(BOT)).rounds == 1


async def test_speech_guard_only_applies_to_first_person(db, make_configs):
    env = build_story(db, make_configs({"story": {"enabled": True, "narration": "second"}}))
    await start_story(env)
    script_scene(env, turns=[{**PLAIN_TURN, "narration": SPOKEN}])
    assert (await env.director.run(BOT)).rounds == 1


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
    assert "【眼前】你在便利店等雨停，后来撑着伞走回了合租屋。" in env.llm.prompts("actor")[1]

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
    ("asked", "minutes"), [(0, 0), (1, 1), (45, 45), (99999, 99999)]
)
async def test_next_wake_is_not_clamped_by_default(env, asked, minutes):
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
            "tree": {"threads": [{"title": "伏笔", "people": [], "canon": [], "seed": [], "scenes": []}]},
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
    assert await env.store.notes(BOT) == []
    assert json.loads((await env.store.state(BOT))["tree"])["threads"][0]["title"] == "伏笔"


async def test_notes_and_scene_summaries_reach_the_next_scene(env):
    await start_story(env)
    script_scene(env)
    await env.director.run(BOT)
    env.clock.advance(15 * MINUTE)
    script_scene(env)
    await env.director.run(BOT)
    opening_prompt = env.llm.prompts("gm")[3]
    assert "她是青屿大学的大三学生" in opening_prompt
    assert "## 暗线" in opening_prompt
    assert "室友阿澄在瞒着她找工作" not in opening_prompt
    assert "姓周，话少" not in opening_prompt
    assert "地点：便利店；在做：躲雨" in opening_prompt
    # 上一场的结尾原样带上，好让新的一场接着写；但仍然只有她外显的言行
    assert (
        "# 上一场是这样收住的（接着它往下写，不要重演）\n"
        "【她】我走进便利店，想买把伞。\n【眼前】店里只剩一把黑伞，你买下了它。"
    ) in opening_prompt
    assert "其实不想回去" not in "\n".join(env.llm.prompts("gm"))
    assert "上一场是这样收住的" not in env.llm.prompts("gm")[0]  # 第一场没有上一场


async def test_budget_line_tells_the_refill_pace(env):
    await start_story(env)
    script_scene(env)
    await env.director.run(BOT)
    assert "唤醒额度：剩 10.0 次（平均每 144 分钟回补 1 次，最多攒 30 次）" in env.llm.prompts("gm")[0]


async def test_long_narration_is_cut_at_a_sentence_boundary(env):
    await start_story(env)
    long = "你沿着北门夜市往回走。" * 60  # 660 字，超过 400 的上限
    script_scene(env, opening={**OPENING, "narration": long})
    await env.director.run(BOT)
    scene = await env.db.fetchone("SELECT transcript FROM story_scenes")
    first = json.loads(scene["transcript"])[0]["text"]
    assert len(first) <= 400 and first.endswith("。")


async def test_lookup_results_come_back_on_the_next_call(env):
    await start_story(env)
    script_scene(env, opening={**OPENING, "lookup": ["阿澄"]})
    await env.director.run(BOT)
    assert "# 你查到的\n- [人物] 阿澄：她的室友，学美术，作息混乱。" in env.llm.prompts("gm")[1]


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

    monkeypatch.setattr(env.store, "update_state", boom)
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
    # 重试时告诉模型上一次错在哪
    assert provider.requests[1]["prompt"] == (
        "用户\n\n# 上一次输出不合格\n上一次没有只输出一个 JSON 对象。\n请按要求重新输出。"
    )


async def test_llm_retry_carries_the_validators_reason(env):
    provider = FakeProvider(['{"narration": "小明发来消息"}', '{"narration": "手机没有动静"}'])

    def validate(value):
        return "叙述里不能出现小明" if "小明" in value["narration"] else True

    value, attempts = await make_llm(provider, env.stack.audit).ask_json(
        "gm", BOT, "系统", "用户", validate=validate
    )
    assert value == {"narration": "手机没有动静"} and attempts == 2
    assert "# 上一次输出不合格\n叙述里不能出现小明" in provider.requests[1]["prompt"]


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


def _gm_thinking(env) -> list[bool]:
    return [
        flag
        for (role, _, _), flag in zip(env.llm.calls, env.llm.thinking)
        if role == "gm"
    ]


async def test_only_the_closing_call_asks_for_thinking(env):
    await start_story(env)
    script_scene(env)
    await env.director.run(BOT)
    assert _gm_thinking(env) == [False, False, True]


async def test_thinking_off_never_asks(db, make_configs):
    env = build_story(db, make_configs({"story": {"enabled": True, "thinking": "off"}}))
    await start_story(env)
    script_scene(env)
    await env.director.run(BOT)
    assert _gm_thinking(env) == [False, False, False]


async def test_scene_thinking_turns_on_only_when_something_is_unfinished(db, make_configs):
    env = build_story(db, make_configs({"story": {"enabled": True, "thinking": "scene"}}))
    await start_story(env)
    env.llm.push("gm", {"think": True, "why": "燃气费没交"})
    script_scene(env)
    await env.director.run(BOT)
    assert _gm_thinking(env) == [False, True, True, True]


async def test_scene_thinking_stays_off_when_nothing_is_unfinished(db, make_configs):
    env = build_story(db, make_configs({"story": {"enabled": True, "thinking": "scene"}}))
    await start_story(env)
    env.llm.push("gm", {"think": False, "why": "第一场"})
    script_scene(env)
    await env.director.run(BOT)
    assert _gm_thinking(env) == [False, False, False, False]


async def test_plan_puts_constraints_in_context_and_does_not_think_while_playing(db, make_configs):
    env = build_story(db, make_configs({"story": {"enabled": True, "thinking": "plan"}}))
    await start_story(env)
    env.llm.push("gm", {"think": True, "why": "燃气费没交"})
    env.llm.push(
        "gm",
        {"established": "住在合租屋", "unresolved": "燃气费没交", "consequences": "去交或再拖"},
    )
    script_scene(env)
    await env.director.run(BOT)
    # 判断和规划都不在执行时开思考；规划那一次本身要思考
    assert _gm_thinking(env) == [False, True, False, False, False]
    opening = env.llm.prompts("gm")[2]
    assert "# 这场的约束（她看不到）" in opening
    assert "燃气费没交" in opening
    assert "这场的约束" not in "\n".join(note["body"] for note in await env.store.notes(BOT))


async def test_thinking_uses_the_direct_model_and_skips_without_one(env):
    provider = FakeProvider(['{"narration": "好"}'])
    seen: list[tuple[str, str]] = []

    async def direct(system, user):
        seen.append((system, user))
        return '{"narration": "想过了"}'

    llm = StoryLLM(
        lambda role, bot_id: _provider(provider), env.stack.audit, direct=direct
    )
    value, _ = await llm.ask_json("gm", BOT, "系统", "用户", thinking=True)
    assert value == {"narration": "想过了"} and seen == [("系统", "用户")]
    assert provider.requests == []

    skipped = StoryLLM(lambda role, bot_id: _provider(provider), env.stack.audit)
    value, _ = await skipped.ask_json("gm", BOT, "系统", "用户", thinking=True)
    assert value == {"narration": "好"} and len(provider.requests) == 1


async def _provider(provider):
    return provider


# --------------------------------------------------------------------- 与聊天连通
async def _session(env, name="小明", umo="umo1", kind="private"):
    await env.db.execute(
        "INSERT INTO sessions(bot_id, umo, name, type, last_message_at) VALUES(?,?,?,?,?)",
        (BOT, umo, name, kind, env.clock.now),
    )


def _queue_scene(env, *, actions, turns, opening=None, closing=None):
    env.llm.push("gm", dict(opening or OPENING))
    for action, turn in zip(actions, turns, strict=True):
        env.llm.push("actor", dict(action))
        env.llm.push("gm", dict(turn))
    env.llm.push("gm", dict(closing or CLOSING))
    env.stack.text.push(dict(MEMORY))


async def test_display_name_miss_then_one_retry_with_the_session_id(env):
    await start_story(env)
    await _session(env)
    _queue_scene(
        env,
        actions=[
            {**ACTION, "messages": [{"to": "不存在的人", "text": "在吗"}]},
            {**ACTION, "messages": [{"to": "umo1", "text": "在吗"}]},
        ],
        turns=[{**PLAIN_TURN, "end": False}, {**PLAIN_TURN, "end": True}],
    )
    sent: list[tuple[str, str]] = []

    async def send(umo, text):
        sent.append((umo, text))
        return True

    env.director.send_text = send
    result = await env.director.run(BOT)
    assert sent == [("umo1", "在吗")]
    assert result.rounds == 2
    assert "没有这个人" in env.llm.prompts("actor")[1]
    state = await env.store.state(BOT)
    assert state["share_tokens"] == pytest.approx(19)


async def test_report_does_not_spend_the_share_bucket(env):
    await start_story(env)
    await _session(env)
    await env.store.add_intent(BOT, "umo1", "小明", "去问燃气费", env.clock.now)
    _queue_scene(
        env,
        actions=[dict(ACTION), {**ACTION, "messages": [{"to": "小明", "text": "问过了"}]}],
        turns=[
            {**PLAIN_TURN, "settled": [1], "end": False},
            {**PLAIN_TURN, "end": True},
        ],
    )

    async def send(umo, text):
        return True

    env.director.send_text = send
    await env.director.run(BOT)
    state = await env.store.state(BOT)
    assert state["share_tokens"] == pytest.approx(20)
    row = await env.db.fetchone("SELECT kind FROM story_sends")
    assert row["kind"] == "report"
    chat = await env.db.fetchone(
        "SELECT source, status FROM events WHERE umo='umo1' AND source='chat' ORDER BY id DESC"
    )
    assert chat["status"] == "closed"


async def test_unread_keeps_only_the_latest(env):
    await start_story(env)
    await env.service.note_unread(BOT, "umo1", "小明", "第一句")
    await env.service.note_unread(BOT, "umo2", "小红", "第二句")
    rows = await env.store.unreads(BOT)
    assert [row["text"] for row in rows] == ["第二句"]


async def test_remote_name_is_allowed_until_the_close_marks_it(db, make_configs):
    env = with_name_guard(db, make_configs)
    await start_story(env)
    await chat_with(env.db)
    await env.store.add_remote(BOT, "umo1", "小明", "parcel", "我给你寄了茶", env.clock.now)
    script_scene(env, opening={**OPENING, "narration": "门口放着一盒茶，是小明寄来的。"})
    result = await env.director.run(BOT)
    assert result.rounds == 1
    assert len(await env.store.open_remotes(BOT)) == 1

    script_scene(
        env,
        opening={**OPENING, "narration": "茶还在门口。"},
        closing={**CLOSING, "remote": [{"id": 1, "outcome": "到了"}]},
    )
    await env.director.run(BOT)
    assert await env.store.open_remotes(BOT) == []


async def test_quoted_speech_is_still_blocked_when_the_name_is_allowed(db, make_configs):
    env = with_name_guard(db, make_configs)
    await start_story(env)
    await chat_with(env.db)
    await env.store.add_remote(BOT, "umo1", "小明", "parcel", "我给你寄了茶", env.clock.now)
    script_scene(env, opening={**OPENING, "narration": "手机亮了，小明：「睡了没」。"})
    with pytest.raises(SceneFailed, match="原话"):
        await env.director.run(BOT)


async def test_omitted_wake_asks_once_then_stays_asleep(env):
    await start_story(env)
    closing = {key: value for key, value in CLOSING.items() if key != "next_wake_minutes"}
    script_scene(env, closing=closing)
    env.llm.push("gm", {"summary": "还是没写"})
    result = await env.director.run(BOT)
    assert result.next_wake_at == 0
    state = await env.store.state(BOT)
    assert state["next_wake_at"] is None


async def test_omitted_wake_accepts_the_follow_up(env):
    await start_story(env)
    closing = {key: value for key, value in CLOSING.items() if key != "next_wake_minutes"}
    script_scene(env, closing=closing)
    env.llm.push("gm", {"next_wake_minutes": 15})
    result = await env.director.run(BOT)
    assert result.next_wake_at == env.clock.now + 15 * MINUTE


async def test_early_wake_moves_the_appointment_without_spending_a_token(env):
    await start_story(env)
    before = await env.store.state(BOT)
    later = env.clock.now + 5 * 60 * MINUTE
    await env.store.update_state(BOT, env.clock.now, next_wake_at=later)
    text = await env.service.propose(BOT, "umo1", "小明", "去问燃气费", wake_now=True)
    assert text == "我现在就去。"
    state = await env.store.state(BOT)
    assert state["next_wake_at"] == env.clock.now
    assert state["tokens"] == before["tokens"]
    assert (await env.store.open_intents(BOT))[0]["body"] == "去问燃气费"


async def test_early_wake_without_tokens_only_queues(env):
    await start_story(env)
    await env.store.update_state(BOT, env.clock.now, tokens=0, tokens_at=env.clock.now)
    later = env.clock.now + 30 * MINUTE
    await env.store.update_state(BOT, env.clock.now, next_wake_at=later)
    text = await env.service.propose(BOT, "umo1", "小明", "去问", wake_now=True)
    assert "走不开" in text
    state = await env.store.state(BOT)
    assert state["next_wake_at"] == later
    assert state["tokens"] == 0


async def test_seen_message_records_only_a_clear_remote(env):
    await start_story(env)
    env.stack.jev.ask_value = {
        "parcel": {"noul": 0.2},
        "food": {"noul": 0.1},
        "money": {"noul": 0.95},
    }
    await env.service.note_seen_message(BOT, "umo1", "小明", "钱转你了")
    rows = await env.store.open_remotes(BOT)
    assert rows[0]["kind"] == "money"
    env.stack.jev.ask_value = None
    env.stack.jev.noul_value = None
    await env.service.note_seen_message(BOT, "umo1", "小明", "在吗")
    assert len(await env.store.open_remotes(BOT)) == 1


# --------------------------------------------------------------------- 网页搜索
SNIPPET = "气象台发布青屿暴雨橙色预警，链接 https://example.test/rain"


class ToolProvider:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests = []

    async def text_chat(self, **kwargs):
        self.requests.append(kwargs)
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def tool_call(query: str = "青屿天气"):
    return SimpleNamespace(
        completion_text="",
        tools_call_name=["web_search"],
        tools_call_args=[{"query": query}],
        tools_call_ids=["call-1"],
    )


def chat_text(text: str):
    return SimpleNamespace(completion_text=text, tools_call_name=None)


class FakeSearch:
    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[str, dict]] = []
        self.func_tool = object()
        self.packed: list[list[str]] = []

    async def run(self, name, arguments):
        self.calls.append((name, arguments))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def pack(self, response, contents):
        self.packed.append(list(contents))
        return {"contents": list(contents)}


def _llm(provider, audit, direct=None):
    async def resolver(role, bot_id):
        return provider

    return StoryLLM(resolver, audit, direct=direct)


def test_gm_prompt_asks_for_perceivable_search_results():
    text = prompts.gm_system()
    assert "天气" in text and "当地事件" in text
    assert "我搜索了" in text and "不要编造" in text


async def test_search_once_then_json_and_the_snippet_stays_out_of_the_transcript(db, make_configs):
    env = build_story(db, make_configs(STORY_ON))
    provider = ToolProvider(
        [
            tool_call(),
            chat_text(json.dumps({**OPENING, "narration": "窗外在下雨。"}, ensure_ascii=False)),
            chat_text(json.dumps(ACTION, ensure_ascii=False)),
            chat_text(json.dumps(PLAIN_TURN, ensure_ascii=False)),
            chat_text(json.dumps(CLOSING, ensure_ascii=False)),
        ]
    )
    search = FakeSearch(SNIPPET)
    seen_bots: list[str] = []
    env.director.llm = _llm(provider, env.stack.audit)
    env.director._search_tools = lambda bot_id: (seen_bots.append(bot_id), search)[1]
    env.stack.text.push(dict(MEMORY))
    await start_story(env)
    result = await env.director.run(BOT)
    assert result.rounds == 1
    assert seen_bots == [BOT]
    assert search.calls == [("web_search", {"query": "青屿天气"})]
    assert SNIPPET[:20] in search.packed[0][0]
    stored = await env.db.fetchall("SELECT text FROM event_turns")
    blob = "\n".join(row["text"] for row in stored)
    events = await env.db.fetchall("SELECT fragment, summary FROM events WHERE source='story'")
    blob += "\n" + "\n".join((row["fragment"] or "") + (row["summary"] or "") for row in events)
    assert "气象台" not in blob
    assert "example.test" not in blob


async def test_search_failure_still_finishes_the_scene(db, make_configs):
    env = build_story(db, make_configs(STORY_ON))
    provider = ToolProvider(
        [
            tool_call(),
            chat_text(json.dumps(OPENING, ensure_ascii=False)),
            chat_text(json.dumps(ACTION, ensure_ascii=False)),
            chat_text(json.dumps(PLAIN_TURN, ensure_ascii=False)),
            chat_text(json.dumps(CLOSING, ensure_ascii=False)),
        ]
    )
    search = FakeSearch(RuntimeError("搜索挂了"))
    env.director.llm = _llm(provider, env.stack.audit)
    env.director._search_tools = lambda: search
    env.stack.text.push(dict(MEMORY))
    await start_story(env)
    result = await env.director.run(BOT)
    assert result.rounds == 1
    assert search.calls and search.packed[0] == ["没搜到"]


async def test_tool_results_are_clipped_before_they_return(env):
    provider = ToolProvider([tool_call(), chat_text('{"narration": "好"}')])
    search = FakeSearch("雨" * 2000)
    await _llm(provider, env.stack.audit).ask_json("gm", BOT, "系统", "用户", search=search)
    assert len(search.packed[0][0]) == 1501
    assert search.packed[0][0].endswith("…")


async def test_third_tool_round_is_not_executed(env):
    provider = ToolProvider([tool_call(), tool_call(), chat_text('{"narration": "好"}')])
    search = FakeSearch("第一页")
    value, _attempts = await _llm(provider, env.stack.audit).ask_json(
        "gm", BOT, "系统", "用户", search=search
    )
    assert value == {"narration": "好"}
    assert len(search.calls) == 2
    assert "func_tool" not in provider.requests[2]
    assert "没搜到" in provider.requests[2]["prompt"]


async def test_close_hands_search_notes_to_the_direct_model(env):
    provider = ToolProvider(
        [tool_call(), chat_text('{"narration": "不该采用这段"}')]
    )
    search = FakeSearch(SNIPPET)
    seen: list[str] = []

    async def direct(system, user):
        seen.append(user)
        return '{"narration": "窗外在下雨。"}'

    value, _attempts = await _llm(provider, env.stack.audit, direct=direct).ask_json(
        "gm", BOT, "系统", "用户", search=search, thinking=True, tools_then_think=True
    )
    assert value == {"narration": "窗外在下雨。"}
    assert SNIPPET in seen[0]
    assert len(search.calls) == 1


def _line(seed: str) -> str:
    text = seed + "我会先把眼前这件事从头看到尾，再决定要不要插手，不喜欢被人催着马上表态一下。"
    assert 40 <= len(text) <= 90, len(text)
    return text


def _promote_args(name: str = "阿澄") -> dict:
    return {
        "name": name,
        "relationship": "她的室友，一起合租。",
        "core": _line("平常"),
        "edge": _line("被问到"),
        "tension": _line("夜里"),
    }


def test_bad_persona_is_not_nominated():
    progress = SceneProgress(started_at=NOW, cause="manual")
    tools = CastTools(progress, [], cap=3)
    text = tools.promote({"name": "阿澄", "relationship": "室友", "core": "太短"})
    assert "没有写成" in text
    assert progress.nominations == []


def test_promotion_over_cap_is_refused():
    progress = SceneProgress(started_at=NOW, cause="manual")
    roster = [{"name": "甲", "status": "active"}, {"name": "乙", "status": "active"}, {"name": "丙", "status": "active"}]
    tools = CastTools(progress, roster, cap=3)
    text = tools.promote(_promote_args("丁"))
    assert "空位已满" in text
    assert progress.nominations == []


async def test_nomination_is_created_only_when_the_scene_commits(env):
    progress = SceneProgress(started_at=env.clock.now, cause="manual")
    tools = CastTools(progress, [], cap=3)
    assert "记下了" in tools.promote(_promote_args())
    assert await env.store.npcs(BOT) == []
    await env.director._finish_nominations(BOT, progress, env.clock.now, 3)
    rows = await env.store.npcs(BOT)
    assert rows[0]["name"] == "阿澄" and rows[0]["status"] == "active"
    fact = await env.db.fetchone("SELECT text FROM facts WHERE bot_id=?", (rows[0]["npc_bot_id"],))
    assert fact["text"] == "她的室友，一起合租。"


async def test_a_failed_scene_does_not_create_the_nominee(env):
    await start_story(env)
    spoken = json.dumps({**PLAIN_TURN, "narration": SPOKEN}, ensure_ascii=False)
    provider = ToolProvider(
        [
            SimpleNamespace(
                completion_text="",
                tools_call_name=["promote_npc"],
                tools_call_args=[_promote_args()],
                tools_call_ids=["p1"],
            ),
            chat_text(json.dumps(OPENING, ensure_ascii=False)),
            chat_text(json.dumps(ACTION, ensure_ascii=False)),
            chat_text(spoken),
            chat_text(spoken),
        ]
    )
    env.director.llm = _llm(provider, env.stack.audit)
    with pytest.raises(SceneFailed):
        await env.director.run(BOT)
    assert await env.store.npcs(BOT) == []


async def test_present_npc_acts_every_round_and_inner_stays_out(env):
    await start_story(env)
    await env.store.insert_npc(BOT, "npc:bot1:1", "阿澄", "室友", env.clock.now)
    await env.db.execute(
        "UPDATE story_npcs SET last_seen_at=? WHERE npc_bot_id=?",
        (env.clock.now, "npc:bot1:1"),
    )
    script_scene(
        env,
        opening={**OPENING, "present": ["阿澄"]},
        turns=[
            {**PLAIN_TURN, "end": False, "narration": "雨还在下。"},
            PLAIN_TURN,
        ],
    )
    env.llm.push("npc", {"inner": "心里不想出门", "action": "阿澄把伞递过来。"})
    env.llm.push("npc", {"inner": "还是回去", "action": "阿澄先上楼了。"})
    await env.director.run(BOT)
    npc_calls = [call for call in env.llm.calls if call[0] == "npc"]
    assert len(npc_calls) == 2
    assert all("其实不想回去" not in call[2] for call in npc_calls)
    stored = "\n".join(row["text"] for row in await env.db.fetchall("SELECT text FROM event_turns"))
    assert "阿澄把伞递过来。" in stored
    assert "心里不想出门" not in stored


async def test_unknown_names_are_played_by_the_background_model(env):
    await start_story(env)
    script_scene(env, opening={**OPENING, "present": ["路人甲"]})
    result = await env.director.run(BOT)
    assert result.rounds == 1
    assert [call for call in env.llm.calls if call[0] == "npc"] == []


async def test_a_dormant_npc_wakes_when_there_is_a_slot(env):
    await start_story(env)
    await env.store.insert_npc(BOT, "npc:bot1:1", "阿澄", "室友", env.clock.now)
    await env.db.execute(
        "UPDATE story_npcs SET status='dormant', last_seen_at=? WHERE name='阿澄'",
        (env.clock.now,),
    )
    script_scene(env, opening={**OPENING, "present": ["阿澄"]})
    env.llm.push("npc", {"inner": "醒了", "action": "阿澄从房间里探出头。"})
    await env.director.run(BOT)
    row = await env.db.fetchone("SELECT status FROM story_npcs WHERE name='阿澄'")
    assert row["status"] == "active"
    assert any(call[0] == "npc" for call in env.llm.calls)


async def test_a_dormant_npc_stays_asleep_when_the_roster_is_full(env):
    await start_story(env)
    now = env.clock.now
    for index, name in enumerate(("甲", "乙", "丙"), start=1):
        await env.store.insert_npc(BOT, f"npc:bot1:{index}", name, "同学", now)
        await env.db.execute(
            "UPDATE story_npcs SET last_seen_at=? WHERE name=?", (now, name)
        )
    await env.store.insert_npc(BOT, "npc:bot1:4", "阿澄", "室友", now)
    await env.db.execute(
        "UPDATE story_npcs SET status='dormant', last_seen_at=? WHERE name='阿澄'",
        (now,),
    )
    script_scene(env, opening={**OPENING, "present": ["阿澄"]})
    await env.director.run(BOT)
    row = await env.db.fetchone("SELECT status FROM story_npcs WHERE name='阿澄'")
    assert row["status"] == "dormant"
    assert [call for call in env.llm.calls if call[0] == "npc"] == []


async def test_idle_npcs_sleep_before_the_scene(env):
    await start_story(env)
    await env.store.insert_npc(BOT, "npc:bot1:1", "阿澄", "室友", env.clock.now)
    await env.db.execute(
        "UPDATE story_npcs SET last_seen_at=? WHERE name='阿澄'",
        (env.clock.now - 15 * 86_400_000,),
    )
    script_scene(env)
    await env.director.run(BOT)
    row = await env.db.fetchone("SELECT status FROM story_npcs WHERE name='阿澄'")
    assert row["status"] == "dormant"


async def test_invalid_world_seed_is_not_saved(env):
    await start_story(env)
    before = (await env.store.seed_form(BOT))["outline"]
    with pytest.raises(Exception):
        await env.service.import_seed(
            BOT, {"version": "world_seed/v1", "title": "坏", "outline": "太短", "entries": []}
        )
    assert (await env.store.seed_form(BOT))["outline"] == before


async def test_actor_receives_known_world_knowledge_and_gm_canon_defaults_gm_only(env):
    seed = world_seed()
    seed["known_outline"] = "我知道这个世界存在时能，我在青溪县小院值守。"
    seed["entries"] = [
        {
            "category": "其他",
            "name": "时能与宇宙流速",
            "body": "第四维度时间流动蕴含的能量称为时能。",
            "visibility": "known",
        },
        {
            "category": "其他",
            "name": "冥蛇族与能力封印",
            "body": "白映风本人不知情的血脉秘密。",
            "visibility": "gm_only",
        },
    ]
    await env.service.import_seed(BOT, seed)
    await env.service.enable(BOT)
    env.stack.jev.available = False

    opening = {
        "backfill": "",
        "narration": "值班室监测终端响了一声，屏幕上的时能曲线出现轻微漂移。",
        "lookup": [],
    }
    closing = {
        **CLOSING,
        "canon": [
            {"name": "神秘黑盒", "category": "物品", "body": "幕后留下的未知装置。"},
            {
                "name": "老街五金店",
                "category": "地点",
                "body": "老板姓陈，专卖零碎金属件。",
                "visibility": "known",
            },
        ],
    }
    script_scene(env, opening=opening, closing=closing)
    await env.director.run(BOT)

    actor_calls = [call for call in env.llm.calls if call[0] == "actor"]
    assert actor_calls
    _, actor_sys, actor_usr = actor_calls[0]
    assert "# 我知道的这个世界" in actor_sys
    assert "我知道这个世界存在时能" in actor_sys
    assert "时能与宇宙流速" in actor_sys
    assert "memory_search" not in actor_sys  # 故事 Actor 没有聊天工具提示
    assert "冥蛇族" not in actor_sys
    assert "【我知道的世界常识" in actor_usr
    assert "第四维度时间流动蕴含的能量称为时能" in actor_usr
    assert "冥蛇族" not in actor_usr

    canon = {row["name"]: row["visibility"] for row in await env.store.canon(BOT)}
    assert canon["神秘黑盒"] == "gm_only"
    assert canon["老街五金店"] == "known"
