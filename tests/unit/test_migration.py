from __future__ import annotations

import sqlite3

from astrbot_plugin_shadow_memory.core.db import Database


async def test_fresh_database_sets_schema_version(workdir):
    path = workdir / "fresh.db"
    db = Database(path)
    await db.connect()
    assert await db.scalar("PRAGMA user_version") == 2
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
    assert await second.scalar("PRAGMA user_version") == 2
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
    assert {"storage_strength", "retrievability", "last_recalled_at", "merged_into"} <= columns
    assert await db.scalar("PRAGMA user_version") == 2
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


async def test_kv_upsert(db):
    await db.kv_set("b", "k", "1", 1)
    await db.kv_set("b", "k", "2", 2)
    assert await db.kv_get("b", "k") == "2"
    assert await db.kv_get("b", "missing", "fallback") == "fallback"
