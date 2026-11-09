from __future__ import annotations

import pytest

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.interaction.notes import NoteService


@pytest.fixture
def notes(db):
    return NoteService(db)


async def test_write_note_and_read(notes):
    result = await notes.write("bot1", title="关于海", content="海风很咸，但我喜欢。")
    row = await notes.read("bot1", result["id"])
    assert row["title"] == "关于海"
    assert "咸" in row["body"]


async def test_diary_gets_date_title(notes):
    result = await notes.write("bot1", kind="diary", content="今天风很好。")
    assert result["kind"] == "diary"
    assert result["title"]


async def test_empty_content_rejected(notes):
    with pytest.raises(ValueError):
        await notes.write("bot1", content="   ")


async def test_search_by_keyword(notes):
    await notes.write("bot1", title="海边", content="风很大，我在沙滩上走了很久。")
    await notes.write("bot1", title="别的", content="今天吃了面。")
    rows = await notes.search("bot1", "海边")
    assert len(rows) == 1
    assert rows[0]["title"] == "海边"
    assert "excerpt" in rows[0]


async def test_search_single_char(notes):
    await notes.write("bot1", title="风", content="风很大。")
    assert len(await notes.search("bot1", "风")) == 1


async def test_list_by_kind_and_limit(notes):
    await notes.write("bot1", kind="note", content="笔记一")
    await notes.write("bot1", kind="diary", content="日记一")
    assert len(await notes.list("bot1", "note")) == 1
    assert len(await notes.list("bot1", limit=1)) == 1


async def test_read_missing(notes):
    assert await notes.read("bot1", 99) is None


async def test_notes_are_bot_scoped(notes):
    result = await notes.write("bot1", content="只属于这个机器人")
    assert await notes.read("bot2", result["id"]) is None
