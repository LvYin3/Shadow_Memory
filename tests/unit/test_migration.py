from __future__ import annotations

import asyncio
import sqlite3

import pytest

from astrbot_plugin_shadow_memory.core.db import SCHEMA_VERSION, Database


async def test_fresh_database_sets_schema_version(workdir):
    path = workdir / "fresh.db"
    db = Database(path)
    await db.connect()
    assert await db.scalar("PRAGMA user_version") == SCHEMA_VERSION
    tables = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    names = {row["name"] for row in tables}
    assert {
        "events",
        "facts",
        "persona_entries",
        "qzone_logs",
        "media_items",
        "media_logs",
        "kv",
        "story_state",
        "story_scenes",
        "story_canon",
        "story_notes",
        "story_intents",
        "story_remotes",
        "story_unreads",
        "story_sends",
        "story_npcs",
        "image_refs",
        "image_temp",
        "image_logs",
    } <= names
    await db.close()


async def test_connect_is_idempotent(workdir):
    path = workdir / "twice.db"
    first = Database(path)
    await first.connect()
    await first.execute("INSERT INTO kv(bot_id, key, value, updated_at) VALUES('b','k','v',1)")
    await first.close()

    second = Database(path)
    await second.connect()
    assert await second.kv_get("b", "k") == "v"
    assert await second.scalar("PRAGMA user_version") == SCHEMA_VERSION
    await second.close()


async def test_v1_database_gets_new_columns(workdir):
    path = workdir / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          bot_id TEXT NOT NULL,
          umo TEXT NOT NULL,
          started_at INTEGER NOT NULL,
          ended_at INTEGER,
          fragment TEXT,
          salience INTEGER,
          status TEXT NOT NULL DEFAULT 'open',
          created_at INTEGER NOT NULL
        );
        PRAGMA user_version=1;
        """
    )
    conn.execute(
        "INSERT INTO events(bot_id, umo, started_at, fragment, salience, status, created_at) "
        "VALUES('bot1','umo1',1,'旧事件',2,'closed',1)"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    await db.connect()
    columns = {
        row["name"] for row in await db.fetchall("PRAGMA table_info(events)")
    }
    assert {
        "storage_strength",
        "retrievability",
        "last_recalled_at",
        "merged_into",
        "source",
    } <= columns
    assert await db.scalar("SELECT source FROM events LIMIT 1") == "chat"
    assert await db.scalar("PRAGMA user_version") == SCHEMA_VERSION
    row = await db.fetchone("SELECT fragment FROM events LIMIT 1")
    assert row["fragment"] == "旧事件"
    indexes = await db.fetchall("SELECT name FROM sqlite_master WHERE type='index'")
    assert any("idx_events_r" in row["name"] for row in indexes)
    await db.close()


async def test_foreign_keys_cascade(db):
    event_id = await db.execute(
        "INSERT INTO events(bot_id, umo, kind, started_at, status, created_at) "
        "VALUES('b','u','episodic',1,'closed',1)"
    )
    await db.execute(
        "INSERT INTO event_turns(event_id, seq, role, text, ts) VALUES(?,1,'user','hi',1)",
        (event_id,),
    )
    await db.execute("DELETE FROM events WHERE id=?", (event_id,))
    assert await db.scalar("SELECT COUNT(*) FROM event_turns") == 0


async def test_transaction_rolls_back(db):
    try:
        async with db.transaction():
            await db.execute(
                "INSERT INTO kv(bot_id, key, value, updated_at) VALUES('b','k','v',1)"
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert await db.kv_get("b", "k") is None


async def test_transaction_does_not_absorb_other_tasks(db):
    """别的协程在事务期间的写入要等锁，不能被卷进事务随回滚一起丢。"""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        try:
            async with db.transaction():
                await db.execute(
                    "INSERT INTO kv(bot_id, key, value, updated_at) VALUES('b','inside','v',1)"
                )
                entered.set()
                await release.wait()
                raise RuntimeError("boom")
        except RuntimeError:
            pass

    async def outsider() -> None:
        await entered.wait()
        await db.execute(
            "INSERT INTO kv(bot_id, key, value, updated_at) VALUES('b','outside','v',1)"
        )

    holding = asyncio.create_task(holder())
    writing = asyncio.create_task(outsider())
    await entered.wait()
    await asyncio.sleep(0.05)
    assert not writing.done()
    release.set()
    await asyncio.gather(holding, writing)
    assert await db.kv_get("b", "inside") is None
    assert await db.kv_get("b", "outside") == "v"


async def test_concurrent_transactions_queue(db):
    async def work(key: str) -> None:
        async with db.transaction():
            await db.execute(
                "INSERT INTO kv(bot_id, key, value, updated_at) VALUES('b',?,'v',1)", (key,)
            )
            await asyncio.sleep(0.01)

    await asyncio.gather(work("first"), work("second"))
    assert await db.kv_get("b", "first") == "v"
    assert await db.kv_get("b", "second") == "v"


async def test_nested_transaction_in_same_task_is_rejected(db):
    async with db.transaction():
        with pytest.raises(RuntimeError):
            async with db.transaction():
                pass
    # 外层事务照常提交，锁也已释放
    await db.kv_set("b", "after", "v", 1)
    assert await db.kv_get("b", "after") == "v"


async def test_executemany_is_all_or_nothing(db):
    rows = [("b", "note", "一", 1, 1), ("b", "weird", "二", 1, 1)]  # 第二行违反 kind 的约束
    with pytest.raises(sqlite3.IntegrityError):
        await db.executemany(
            "INSERT INTO notes(bot_id, kind, body, created_at, updated_at) VALUES(?,?,?,?,?)", rows
        )
    assert await db.scalar("SELECT COUNT(*) FROM notes") == 0
    # 出错后连接仍然可用，没有留下半开的事务
    await db.executemany(
        "INSERT INTO notes(bot_id, kind, body, created_at, updated_at) VALUES(?,?,?,?,?)", rows[:1]
    )
    assert await db.scalar("SELECT COUNT(*) FROM notes") == 1
    await db.executemany("DELETE FROM notes WHERE id=?", [])  # 空批次什么也不做
    assert await db.scalar("SELECT COUNT(*) FROM notes") == 1


async def test_executemany_joins_an_open_transaction(db):
    with pytest.raises(RuntimeError):
        async with db.transaction():
            await db.executemany(
                "INSERT INTO kv(bot_id, key, value, updated_at) VALUES(?,?,?,?)",
                [("b", "k1", "v", 1), ("b", "k2", "v", 1)],
            )
            raise RuntimeError("boom")
    assert await db.scalar("SELECT COUNT(*) FROM kv") == 0


async def test_kv_upsert(db):
    await db.kv_set("b", "k", "1", 1)
    await db.kv_set("b", "k", "2", 2)
    assert await db.kv_get("b", "k") == "2"
    assert await db.kv_get("b", "missing", "fallback") == "fallback"
