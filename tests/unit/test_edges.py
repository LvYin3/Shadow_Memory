from __future__ import annotations

import json

import pytest

from astrbot_plugin_shadow_memory.memory.edges import (
    EdgeRepo,
    entities_from_json,
    normalize_entities,
)


def test_normalize_lowercases_and_strips_punctuation():
    assert normalize_entities(["  Alice!! ", "ALICE", "小 明", "？"]) == ["alice", "小明"]


def test_normalize_caps_at_eight():
    values = [f"实体{index}" for index in range(20)]
    assert len(normalize_entities(values)) == 8


def test_normalize_handles_non_strings_and_empty():
    assert normalize_entities([None, "", 3]) == ["3"]
    assert normalize_entities(None) == []


def test_entities_from_json_variants():
    assert entities_from_json('["a", "b"]') == ["a", "b"]
    assert entities_from_json(["a"]) == ["a"]
    assert entities_from_json("not json") == []
    assert entities_from_json(None) == []


async def _make_event(db, repo: EdgeRepo | None = None, entities=None, bot_id="bot1") -> int:
    event_id = await db.execute(
        "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, entities, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            bot_id,
            "umo",
            "episodic",
            1,
            1,
            json.dumps(entities or [], ensure_ascii=False),
            "closed",
            1,
        ),
    )
    if repo is not None and entities:
        await repo.link(bot_id, event_id, entities, 50)
    return event_id


async def test_link_creates_edges_for_shared_entities(db):
    repo = EdgeRepo(db)
    first = await _make_event(db, repo, ["a", "b"])
    second = await _make_event(db)
    touched = await repo.link("bot1", second, ["b", "c"], 100)
    assert touched == 1
    neighbors = await repo.neighbors("bot1", second, 4)
    assert [row["event_id"] for row in neighbors] == [first]


async def test_link_is_idempotent_but_accumulates_weight(db):
    repo = EdgeRepo(db)
    first = await _make_event(db, repo, ["a"])
    second = await _make_event(db)
    await repo.link("bot1", second, ["a"], 100)
    await repo.link("bot1", second, ["a"], 200)
    row = await db.fetchone(
        "SELECT weight FROM event_edges WHERE src_event_id=? AND dst_event_id=?", (first, second)
    )
    assert row["weight"] == 2
    count = await db.scalar("SELECT COUNT(*) FROM event_edges")
    assert count == 1


async def test_degree_limit_prunes_weakest(db):
    repo = EdgeRepo(db)
    base = await _make_event(db, repo, ["a"])
    for index in range(3):
        other = await _make_event(db)
        await repo.link("bot1", other, ["a"], 100 + index)
    await repo.prune("bot1", base, max_degree=1)
    degree = await repo.degree("bot1", [base])
    assert degree[base] == 1


async def test_neighbors_sorted_by_weight(db):
    repo = EdgeRepo(db)
    base = await _make_event(db, repo, ["a"])
    weak = await _make_event(db)
    strong = await _make_event(db)
    await repo.link("bot1", weak, ["a"], 100)
    await repo.link("bot1", strong, ["a"], 200)
    await repo.link("bot1", strong, ["a"], 300)
    rows = await repo.neighbors("bot1", base, 4)
    assert rows[0]["event_id"] == strong
    assert rows[0]["weight"] > rows[1]["weight"]


async def test_deleting_event_cascades_edges(db):
    repo = EdgeRepo(db)
    first = await _make_event(db, repo, ["a"])
    second = await _make_event(db)
    await repo.link("bot1", second, ["a"], 100)
    await db.execute("DELETE FROM events WHERE id=?", (first,))
    count = await db.scalar("SELECT COUNT(*) FROM event_edges")
    assert count == 0


async def test_neighbors_respects_limit(db):
    repo = EdgeRepo(db)
    base = await _make_event(db, repo, ["a"])
    for index in range(6):
        other = await _make_event(db)
        await repo.link("bot1", other, ["a"], 100 + index)
    rows = await repo.neighbors("bot1", base, 2)
    assert len(rows) == 2


@pytest.mark.parametrize("name", ["", "x" * 40])
async def test_long_entity_is_truncated(db, name):
    repo = EdgeRepo(db)
    event = await _make_event(db)
    await repo.link("bot1", event, [name], 100)
    rows = await db.fetchall("SELECT canonical FROM entities")
    assert all(len(str(row["canonical"])) <= 32 for row in rows)


async def test_entities_are_bot_scoped(db):
    repo = EdgeRepo(db)
    first = await _make_event(db, repo, ["a"], bot_id="bot1")
    other = await _make_event(db, None, bot_id="bot2")
    touched = await repo.link("bot2", other, ["a"], 100)
    # 只有同 bot 的实体关联才算共享
    assert touched == 0
    neighbors = await repo.neighbors("bot1", first, 4)
    assert neighbors == []
