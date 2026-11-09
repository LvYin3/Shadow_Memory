from __future__ import annotations

from astrbot_plugin_shadow_memory.interaction.session_buffer import SessionBuffer


async def test_record_and_last(db):
    buffer = SessionBuffer(db)
    await buffer.record("bot1", "umo1", "user", "第一句", "小明")
    await buffer.record("bot1", "umo1", "assistant", "第二句")
    await buffer.record("bot1", "umo2", "user", "别的会话")
    rows = await buffer.last("bot1", "umo1", 10)
    assert [row["text"] for row in rows] == ["第一句", "第二句"]
    assert rows[0]["speaker"] == "小明"
    assert [row["text"] for row in await buffer.last("bot1", "umo2", 10)] == ["别的会话"]


async def test_prune_keeps_recent(db):
    buffer = SessionBuffer(db, keep=20)
    for index in range(25):
        await buffer.record("bot1", "umo1", "user", f"m{index}")
    rows = await buffer.last("bot1", "umo1", 100)
    assert len(rows) == 20
    assert rows[0]["text"] == "m5"
    assert rows[-1]["text"] == "m24"


async def test_empty_text_ignored(db):
    buffer = SessionBuffer(db)
    await buffer.record("bot1", "umo1", "user", "   ")
    assert await buffer.count("bot1", "umo1") == 0


async def test_clear(db):
    buffer = SessionBuffer(db)
    await buffer.record("bot1", "umo1", "user", "x")
    await buffer.clear("bot1", "umo1")
    assert await buffer.count("bot1", "umo1") == 0
