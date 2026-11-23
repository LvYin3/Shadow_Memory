"""背景模型的长期记忆：开场回想更早的场景、lookup 精确检索、场内按她的行动补设定。"""

from __future__ import annotations

import pytest

from astrbot_plugin_shadow_memory.story.card import StatusCard
from astrbot_plugin_shadow_memory.story.recall import PAST_INSTRUCTION, PastScenes, scene_line

from ..fakes import FakeJev
from .story_helpers import (
    BOT,
    HOUR,
    NOW,
    OPENING,
    PLAIN_TURN,
    STORY_ON,
    build_story,
    script_scene,
    start_story,
)

OLD = [
    "她在旧书摊买了一本没有封面的诗集，摊主说下周还会来。",  # 最早
    "她和阿澄吵了一架，因为阿澄又把颜料洗在了水槽里。",
    "她答应帮楼下的周老板看一下午店。",
    "她去图书馆还书，借了两本讲潮汐的书。",
    "她在宿舍楼下喂了一只橘猫。",
    "她洗了衣服，晾在阳台上。",  # 最近
]


class KeywordJev(FakeJev):
    """回想旧场景时：梗概里带关键词的判为有用，其余判为没用。别的判断照旧。"""

    def __init__(self, useful: dict[str, float] | None = None, fail: bool = False) -> None:
        super().__init__()
        self.useful = useful or {}
        self.fail = fail
        self.past_calls: list[tuple[object, dict]] = []

    async def ask(self, state, questions):
        if not any(PAST_INSTRUCTION[:10] in str(q.get("instructions")) for q in questions.values()):
            return await super().ask(state, questions)
        self.past_calls.append((state, questions))
        if self.fail:
            return None
        answers = {}
        for key, question in questions.items():
            text = question["instructions"]
            value = next((v for word, v in self.useful.items() if word in text), 0.1)
            answers[key] = {"noul": value}
        return answers


async def add_scenes(env, summaries=OLD):
    ids = []
    for index, summary in enumerate(summaries):
        started = NOW - (len(summaries) - index) * 2 * HOUR
        ids.append(
            await env.store.insert_scene(
                BOT,
                cause="scheduled",
                status="ok",
                started_at=started,
                ended_at=started + 600_000,
                transcript=[
                    {"role": "narrator", "text": f"旁白{index}"},
                    {"role": "me", "inner": f"心里话{index}", "text": f"我做了第{index}件事"},
                ],
                summary=summary,
            )
        )
    return ids


def story_env(db, make_configs, jev, **story):
    return build_story(db, make_configs({"story": {"enabled": True, **story}}), jev=jev)


# --------------------------------------------------------------------- 开场回想
async def test_older_scenes_judged_useful_are_brought_back(db, make_configs):
    jev = KeywordJev({"阿澄": 0.9, "周老板": 0.7, "诗集": 0.3})
    env = story_env(db, make_configs, jev)
    await start_story(env)
    ids = await add_scenes(env)
    script_scene(env)
    await env.director.run(BOT)

    opening = env.llm.prompts("gm")[0]
    block = opening.split("# 更早的、和此刻可能有关的场景\n")[1].split("\n\n")[0]
    # 过阈值的按时间顺序带上，带编号方便点名去查；没过阈值的不带
    assert block.splitlines()[0].startswith(f"- #{ids[1]} ") and OLD[1] in block
    assert block.splitlines()[1].startswith(f"- #{ids[2]} ") and OLD[2] in block
    assert OLD[0] not in opening
    # 最近三场照旧在“最近几场的梗概”里，不当候选
    assert f"#{ids[5]} " in opening.split("# 最近几场的梗概")[1]
    (state, questions), = jev.past_calls
    assert len(questions) == 3
    assert all(OLD[i] not in str(questions) for i in (3, 4, 5))
    # 判断模型只看外显信息：没有她的人格，也没有她的内心
    assert set(state) == {"now", "her_status", "recent_scenes", "last_ending"}
    assert "心里话" not in str(state)


async def test_past_pick_respects_the_limit_and_counts_one_call(db, make_configs):
    jev = KeywordJev({"她": 0.9})
    env = story_env(db, make_configs, jev, past_scenes=2, recent_scenes=1)
    await start_story(env)
    await add_scenes(env)
    script_scene(env)
    await env.director.run(BOT)
    block = env.llm.prompts("gm")[0].split("# 更早的、和此刻可能有关的场景\n")[1].split("\n\n")[0]
    assert block.count("- #") == 2
    # 候选不止一条，也只问一次
    assert len(jev.past_calls) == 1 and len(jev.past_calls[0][1]) == 5


async def test_falls_back_to_literal_matches_when_the_judge_fails(db, make_configs):
    jev = KeywordJev(fail=True)
    env = story_env(db, make_configs, jev)
    await start_story(env)
    await add_scenes(env)
    # 状态卡里有“周老板”，字面上和“看店”那一场接得上
    await env.store.update_state(
        BOT,
        card=StatusCard(
            where="周老板的店", doing="看店", until=NOW + HOUR, updated_at=NOW - HOUR
        ).to_json(),
    )
    script_scene(env)
    await env.director.run(BOT)
    opening = env.llm.prompts("gm")[0]
    assert "# 更早的、和此刻可能有关的场景" in opening
    assert OLD[2] in opening.split("# 更早的")[1].split("# 最近几场")[0]


async def test_past_scenes_zero_turns_it_off(db, make_configs):
    jev = KeywordJev({"她": 0.9})
    env = story_env(db, make_configs, jev, past_scenes=0)
    await start_story(env)
    await add_scenes(env)
    script_scene(env)
    await env.director.run(BOT)
    assert jev.past_calls == []
    assert "# 更早的" not in env.llm.prompts("gm")[0]


async def test_nothing_to_recall_costs_nothing(db, make_configs):
    jev = KeywordJev({"她": 0.9})
    env = story_env(db, make_configs, jev)
    await start_story(env)
    await add_scenes(env, OLD[:3])  # 都还在“最近三场”里
    script_scene(env)
    await env.director.run(BOT)
    assert jev.past_calls == []


# --------------------------------------------------------------------- lookup 精确检索
async def test_lookup_reads_scenes_notes_and_old_summaries(db, make_configs):
    env = story_env(db, make_configs, KeywordJev())
    await start_story(env)
    ids = await add_scenes(env)
    await env.store.apply_notes(BOT, [{"title": "诗集的来历", "body": "摊主是她外婆的旧识"}], None, NOW)
    script_scene(
        env,
        opening={**OPENING, "lookup": [f"#{ids[0]}", "诗集的来历", "潮汐", "火星", "#9999"]},
    )
    await env.director.run(BOT)
    found = env.llm.prompts("gm")[1].split("# 你查到的\n")[1]
    # #编号：那一场的完整经过，只有她外显的言行，没有内心
    assert f"#{ids[0]} " in found and "我做了第0件事" in found and "心里话0" not in found
    assert "- [笔记] 诗集的来历：摊主是她外婆的旧识" in found
    assert f"- [以前的场景] #{ids[3]} " in found and OLD[3] in found
    assert "（没有查到：火星）" in found
    assert "（没有 #9999 这一场）" in found


async def test_canon_names_still_resolve_first(env_seeded):
    env = env_seeded
    script_scene(env, opening={**OPENING, "lookup": ["阿澄"]})
    await env.director.run(BOT)
    found = env.llm.prompts("gm")[1].split("# 你查到的\n")[1]
    assert found.startswith("- [人物] 阿澄：")
    assert "[以前的场景]" not in found


@pytest.fixture
async def env_seeded(db, make_configs):
    env = story_env(db, make_configs, KeywordJev())
    await start_story(env)
    await add_scenes(env)
    return env


# --------------------------------------------------------------------- 场内补设定
async def test_canon_she_mentions_is_looked_up_for_the_next_ruling(db, make_configs):
    env = story_env(db, make_configs, KeywordJev(), canon_context_items=0)
    await start_story(env)
    await env.store.add_canon(
        BOT, [{"category": "place", "name": "便利店", "body": "北门外，二十四小时营业。"}], None, NOW
    )
    script_scene(env)  # 她的行动是“我走进便利店，想买把伞。”
    await env.director.run(BOT)
    turn_prompt = env.llm.prompts("gm")[1]
    assert "- [地点] 便利店：北门外，二十四小时营业。" in turn_prompt.split("# 你查到的")[1]
    assert "便利店：北门外" not in env.llm.prompts("gm")[0]


async def test_canon_already_in_context_is_not_repeated(db, make_configs):
    env = story_env(db, make_configs, KeywordJev())
    await start_story(env)
    await env.store.add_canon(
        BOT, [{"category": "place", "name": "便利店", "body": "北门外，二十四小时营业。"}], None, NOW
    )
    script_scene(env, turns=[PLAIN_TURN])
    await env.director.run(BOT)
    turn_prompt = env.llm.prompts("gm")[1]
    assert turn_prompt.count("便利店：北门外") == 1


# --------------------------------------------------------------------- 纯逻辑
async def test_search_needs_an_exact_word_hit(db, make_configs):
    env = story_env(db, make_configs, KeywordJev())
    await start_story(env)
    ids = await add_scenes(env)
    past = PastScenes(env.store, KeywordJev(), env.stack.audit)
    assert await past.search(BOT, ["橘猫"]) == [scene_line(await env.store.scene(BOT, ids[4]))]
    assert await past.search(BOT, ["猫粮"]) == []
