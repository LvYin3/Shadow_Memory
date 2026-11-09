from __future__ import annotations

import json

import pytest

from .helpers import build_stack

DAY = 86_400_000


@pytest.fixture
def stack(db, configs):
    return build_stack(db, configs)


async def _event(
    db,
    fragment: str,
    entities: list[str],
    *,
    ended: int = DAY,
    salience: int = 3,
    bot: str = "bot1",
    status: str = "closed",
) -> int:
    return await db.execute(
        "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, fragment, entities, "
        "storage_strength, retrievability, last_recalled_at, salience, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            bot,
            "umo1",
            "episodic",
            ended,
            ended,
            fragment,
            json.dumps(entities, ensure_ascii=False),
            7.0,
            1.0,
            ended,
            salience,
            status,
            ended,
        ),
    )


async def _three_events(stack) -> list[int]:
    return [
        await _event(stack.db, "第一次去海边", ["海边", "风", "小明"], ended=DAY),
        await _event(stack.db, "又说起海边的风", ["海边", "风"], ended=DAY + 1000),
        await _event(stack.db, "海边那天聊了很久", ["海边", "小明"], ended=DAY + 2000),
    ]


async def test_cluster_requires_three_events(stack):
    await _event(stack.db, "a", ["海边", "风"], ended=DAY)
    await _event(stack.db, "b", ["海边", "风"], ended=DAY + 1)
    assert await stack.consolidator.clusters("bot1", DAY + 10) == []


async def test_cluster_groups_shared_entities(stack):
    await _three_events(stack)
    clusters = await stack.consolidator.clusters("bot1", DAY + 10_000)
    assert len(clusters) == 1
    assert len(clusters[0]) == 3


async def test_disabled_consolidation_returns_nothing(stack, make_configs):
    local = build_stack(stack.db, make_configs({"consolidation": {"enabled": False}}))
    await _three_events(local)
    assert await local.consolidator.clusters("bot1", DAY + 10) == []


async def test_run_creates_gist_and_merges_sources(stack):
    sources = await _three_events(stack)
    stack.text.push(
        {
            "gist_fragment": "海边和小明的那阵风，我一直记着",
            "gist_summary": "两次聊到海边与风，小明都在。",
            "entities": ["海边", "风", "小明"],
            "supersede": [],
            "insights": ["我发现我很在意和小明一起经历的小事"],
        }
    )
    result = await stack.service.consolidate("bot1", DAY + 60_000)
    assert result.clusters == 1
    assert result.merged == 1
    assert result.insights == 1
    gist = await stack.db.fetchone("SELECT * FROM events WHERE kind='gist'")
    assert gist["fragment"].startswith("海边")
    assert gist["storage_strength"] == pytest.approx(7.0 * 1.2)
    merged = await stack.db.fetchall(
        "SELECT merged_into FROM events WHERE id IN (?,?,?)", tuple(sources)
    )
    assert all(row["merged_into"] == gist["id"] for row in merged)
    log = await stack.db.fetchone("SELECT * FROM consolidation_logs")
    assert log["status"] == "ok"
    assert stack.text.tasks[-1] == "consolidate"


async def test_run_supersedes_facts(stack):
    await _three_events(stack)
    fact = await stack.facts.add_manual("bot1", "小明不住在海边", salience=2, now=DAY)
    stack.text.push(
        {
            "gist_fragment": "海边那几天我才知道小明搬家了",
            "gist_summary": "",
            "entities": ["海边", "小明"],
            "supersede": [fact],
            "insights": [],
        }
    )
    result = await stack.service.consolidate("bot1", DAY + 60_000)
    assert result.superseded == 1
    row = await stack.db.fetchone("SELECT valid_to FROM facts WHERE id=?", (fact,))
    assert row["valid_to"] is not None


async def test_cluster_failure_rolls_back(stack):
    sources = await _three_events(stack)
    await stack.facts.add_manual("bot1", "小明不住在海边", salience=2, now=DAY)
    fact_id = await stack.db.scalar("SELECT id FROM facts")
    stack.text.push(
        {
            "gist_fragment": "海边那几天我才知道小明搬家了",
            "gist_summary": "",
            "entities": ["海边", "小明"],
            "supersede": [fact_id],
            "insights": [],
        }
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    stack.facts.supersede = boom
    result = await stack.service.consolidate("bot1", DAY + 60_000)
    assert result.failed == 1
    assert await stack.db.scalar("SELECT COUNT(*) FROM events WHERE kind='gist'") == 0
    merged = await stack.db.fetchall(
        "SELECT merged_into FROM events WHERE id IN (?,?,?)", tuple(sources)
    )
    assert all(row["merged_into"] is None for row in merged)
    log = await stack.db.fetchone("SELECT status FROM consolidation_logs")
    assert str(log["status"]).startswith("failed")


async def test_second_night_does_not_remerge(stack):
    await _three_events(stack)
    payload = {
        "gist_fragment": "海边的那阵风",
        "gist_summary": "",
        "entities": ["海边", "风"],
        "supersede": [],
        "insights": [],
    }
    stack.text.push(payload)
    await stack.service.consolidate("bot1", DAY + 60_000)
    result = await stack.service.consolidate("bot1", DAY + 120_000)
    assert result.clusters == 0
    assert result.merged == 0
