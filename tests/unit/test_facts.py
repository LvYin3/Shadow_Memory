from __future__ import annotations

import pytest

from astrbot_plugin_shadow_memory.core.types import Turn

from .helpers import build_stack


def test_facts_prompt_keeps_other_party_attribution():
    from astrbot_plugin_shadow_memory.memory import prompts

    assert "用候选里出现的名字或昵称指代对方" in prompts.FACTS_SYSTEM
    assert "不要把对方的事写成“我”" in prompts.FACTS_SYSTEM

DAY = 86_400_000


@pytest.fixture
def stack(db, configs):
    return build_stack(db, configs)


async def test_extract_facts_from_candidates(stack):
    await stack.service.observe(
        Turn("bot1", "umo1", "user", "我下周三要去北京出差", DAY, "u1", "小明"),
        display_name="测试群",
        chat_type="group",
    )
    await stack.facts.enqueue("bot1", "umo1", "对方: 下周三去北京", 3, DAY)
    stack.text.push(
        {
            "facts": [
                {
                    "kind": "profile",
                    "subject_id": "u1",
                    "text": "小明下周三要去北京出差",
                    "key_fact_type": "time",
                    "salience": 4,
                },
                {
                    "kind": "preference",
                    "subject_id": "u1",
                    "text": "小明不喜欢太吵的地方",
                    "key_fact_type": "none",
                    "salience": 2,
                },
            ]
        }
    )
    added = await stack.facts.nightly_extract("bot1", DAY)
    assert added == 2
    rows = await stack.db.fetchall("SELECT * FROM facts ORDER BY id")
    assert rows[0]["key_fact"] == 1
    assert rows[0]["storage_strength"] == pytest.approx(9999.0)
    assert rows[1]["key_fact"] == 0
    assert rows[1]["storage_strength"] == pytest.approx(26.25)
    assert stack.text.tasks[-1] == "facts"


async def test_candidates_are_marked_consumed(stack):
    await stack.facts.enqueue("bot1", "umo1", "对方: 我喜欢下雨天", 2, DAY)
    stack.text.push({"facts": []})
    await stack.facts.nightly_extract("bot1", DAY)
    pending = await stack.facts.pending("bot1")
    assert pending == []


async def test_extract_failure_keeps_candidates(stack):
    await stack.facts.enqueue("bot1", "umo1", "对方: 我住在杭州", 3, DAY)
    assert await stack.facts.nightly_extract("bot1", DAY) == 0
    assert len(await stack.facts.pending("bot1")) == 1


async def test_duplicate_fact_is_skipped(stack):
    for _ in range(2):
        await stack.facts.enqueue("bot1", "umo1", "对方: 我在杭州", 3, DAY)
    stack.text.push({"facts": [{"text": "小明在杭州", "kind": "world"}]})
    assert await stack.facts.nightly_extract("bot1", DAY) == 1
    stack.text.push({"facts": [{"text": "小明在杭州", "kind": "world"}]})
    await stack.facts.enqueue("bot1", "umo1", "对方: 小明在杭州", 3, DAY)
    assert await stack.facts.nightly_extract("bot1", DAY) == 0
    assert await stack.db.scalar("SELECT COUNT(*) FROM facts") == 1


async def test_relevant_matches_by_text(stack):
    await stack.facts.add_manual("bot1", "小明住在杭州", salience=3, now=DAY)
    rows = await stack.facts.relevant("bot1", "杭州", DAY + DAY)
    assert rows and "杭州" in rows[0]["text"]


async def test_relevant_skips_faded_fact(stack):
    fact_id = await stack.facts.add_manual("bot1", "小明住在杭州", salience=1, now=DAY)
    await stack.db.execute(
        "UPDATE facts SET storage_strength=1.0, last_recalled_at=? WHERE id=?", (DAY, fact_id)
    )
    rows = await stack.facts.relevant("bot1", "杭州", DAY + 20 * DAY)
    assert rows == []


async def test_supersede_sets_valid_to_and_keeps_row(stack):
    old = await stack.facts.add_manual("bot1", "小明住在杭州", salience=3, now=DAY)
    assert await stack.facts.supersede("bot1", old, DAY + DAY, by=99) is True
    row = await stack.db.fetchone("SELECT * FROM facts WHERE id=?", (old,))
    assert row["valid_to"] == DAY + DAY
    assert row["superseded_by"] == 99
    assert await stack.facts.supersede("bot1", old, DAY + 2 * DAY) is False


async def test_key_fact_cannot_be_superseded(stack):
    key = await stack.facts.add_manual("bot1", "约定周六见面", key_fact=True, now=DAY)
    assert await stack.facts.supersede("bot1", key, DAY + DAY) is False


async def test_search_returns_matching_facts(stack):
    await stack.facts.add_manual("bot1", "小明喜欢喝美式", salience=2, now=DAY)
    rows = await stack.facts.search("bot1", "美式")
    assert rows and "美式" in rows[0]["text"]


async def test_mark_recalled_boosts_strength(stack):
    fact_id = await stack.facts.add_manual("bot1", "小明喜欢喝美式", salience=2, now=DAY)
    await stack.db.execute(
        "UPDATE facts SET last_recalled_at=? WHERE id=?", (DAY, fact_id)
    )
    await stack.facts.mark_recalled([fact_id], DAY + 7 * DAY)
    row = await stack.db.fetchone("SELECT * FROM facts WHERE id=?", (fact_id,))
    assert row["storage_strength"] > 26.25
    assert row["retrievability"] == 1.0
