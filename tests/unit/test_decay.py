from __future__ import annotations

import pytest

from .helpers import build_stack

DAY = 86_400_000


@pytest.fixture
def stack(db, configs):
    return build_stack(db, configs)


async def _event(db, strength: float, last: int, status: str = "closed") -> int:
    return await db.execute(
        "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, fragment, entities, "
        "storage_strength, retrievability, last_recalled_at, salience, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("bot1", "umo1", "episodic", last, last, "旧事", "[]", strength, 1.0, last, 2, status, last),
    )


async def test_decay_multiplies_strength(stack):
    event = await _event(stack.db, 1000.0, DAY)
    result = await stack.service.decay("bot1", DAY + DAY)
    assert result["events"] == 1
    row = await stack.db.fetchone("SELECT * FROM events WHERE id=?", (event,))
    assert row["storage_strength"] == pytest.approx(999.0)
    assert 0.0 < row["retrievability"] <= 1.0


async def test_decay_archives_below_one_day(stack):
    event = await _event(stack.db, 0.5, DAY)
    result = await stack.service.decay("bot1", DAY + DAY)
    assert result["archived"] == 1
    row = await stack.db.fetchone("SELECT status FROM events WHERE id=?", (event,))
    assert row["status"] == "archived"


async def test_decay_writes_back_in_one_batch(stack, monkeypatch):
    """事件再多也只批量写回，不逐行更新；结果与逐行算的一致。"""
    await stack.db.executemany(
        "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, fragment, entities, "
        "storage_strength, retrievability, last_recalled_at, salience, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("bot1", "umo1", "episodic", DAY, DAY, f"旧事{index}", "[]",
             1000.0 if index % 2 else 0.5, 1.0, DAY, 2, "closed", DAY)
            for index in range(300)
        ],
    )
    for index in range(40):
        fact = await stack.facts.add_manual("bot1", f"事实{index}", salience=1, now=DAY)
        if index % 2:
            await stack.db.execute("UPDATE facts SET storage_strength=0.5 WHERE id=?", (fact,))

    single_writes = 0
    original = stack.db.execute

    async def counting(sql, params=()):
        nonlocal single_writes
        single_writes += 1
        return await original(sql, params)

    monkeypatch.setattr(stack.db, "execute", counting)
    result = await stack.service.decay("bot1", 2 * DAY)
    assert single_writes == 0
    assert result == {"events": 300, "archived": 150, "facts_archived": 20}

    rows = await stack.db.fetchall(
        "SELECT status, COUNT(*) AS n, MIN(storage_strength) AS low, MAX(storage_strength) AS high "
        "FROM events GROUP BY status ORDER BY status"
    )
    by_status = {row["status"]: row for row in rows}
    assert by_status["archived"]["n"] == 150
    assert by_status["archived"]["high"] == pytest.approx(0.5 * 0.999)
    assert by_status["closed"]["n"] == 150
    assert by_status["closed"]["low"] == pytest.approx(999.0)
    expected_r = stack.dynamics.r(999.0, DAY, 2 * DAY)
    assert await stack.db.scalar(
        "SELECT MIN(retrievability) FROM events WHERE status='closed'"
    ) == pytest.approx(expected_r)
    assert await stack.db.scalar("SELECT COUNT(*) FROM facts WHERE archived=1") == 20


async def test_decay_with_nothing_to_do(stack):
    assert await stack.service.decay("bot1", DAY) == {
        "events": 0,
        "archived": 0,
        "facts_archived": 0,
    }


async def test_already_archived_is_not_recounted(stack):
    await _event(stack.db, 0.5, DAY, status="archived")
    result = await stack.service.decay("bot1", DAY + DAY)
    assert result["events"] == 0


async def test_open_events_are_not_decayed(stack):
    await _event(stack.db, 1000.0, DAY, status="open")
    result = await stack.service.decay("bot1", DAY + DAY)
    assert result["events"] == 0


async def test_key_fact_is_not_decayed(stack):
    await stack.facts.add_manual("bot1", "约定周六见面", key_fact=True, now=DAY)
    await stack.service.decay("bot1", DAY + 30 * DAY)
    row = await stack.db.fetchone("SELECT * FROM facts")
    assert row["storage_strength"] == pytest.approx(9999.0)
    assert row["archived"] == 0


async def test_normal_fact_decays_and_archives(stack):
    fact = await stack.facts.add_manual("bot1", "小明喜欢喝美式", salience=1, now=DAY)
    await stack.db.execute(
        "UPDATE facts SET storage_strength=1.0, last_recalled_at=? WHERE id=?", (DAY, fact)
    )
    result = await stack.service.decay("bot1", DAY + DAY)
    assert result["facts_archived"] == 1
