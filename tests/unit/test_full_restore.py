from __future__ import annotations

import json
import sqlite3

import pytest

from astrbot_plugin_shadow_memory.memory.seed import SeedError, validate_full

from .helpers import build_stack, text_of_len

NOW = 1_800_000_000_000


@pytest.fixture
def stack(db, configs):
    return build_stack(db, configs)


def seed_payload():
    return {
        "version": "persona_seed/v1",
        "bot_hint": "bot1",
        "persona": {
            "core": [text_of_len(60, "我骨子里")],
            "edge": [text_of_len(60, "在人多的场合")],
            "tension": [text_of_len(60, "我还没想明白")],
        },
        "facts": [
            {
                "kind": "world",
                "subject_id": "u1",
                "text": "小明住在杭州",
                "key_fact_type": "none",
                "salience": 3,
            },
            {
                "kind": "commitment",
                "subject_id": "u1",
                "text": "答应过周末去看海",
                "key_fact_type": "promise",
                "salience": 4,
            },
        ],
        "events": [
            {
                "fragment": "第一次见面是在雨天，她借了我一把伞",
                "summary": "雨天借伞",
                "entities": ["小明", "雨"],
                "occurred_days_ago": 3,
                "salience": 3,
                "participants": [{"id": "u1", "name": "小明"}],
            },
            {
                "fragment": "周末一起去看海，风很大",
                "summary": "",
                "entities": ["海边", "风", "小明"],
                "occurred_days_ago": 10,
                "salience": 4,
                "participants": [],
            },
        ],
    }


async def make_memory(stack, bot_id="bot1"):
    await stack.seed.import_seed(bot_id, seed_payload(), force=True)
    await stack.db.execute(
        "INSERT INTO insights(bot_id, text, source_event_ids, created_at) "
        "VALUES(?,'海边那天的风很大','[]',0)",
        (bot_id,),
    )
    await stack.db.execute(
        "INSERT INTO notes(bot_id, kind, title, body, tags, created_at, updated_at) "
        "VALUES(?,'note','伞','她把伞借给我','[]',0,0)",
        (bot_id,),
    )
    return await stack.service.export_full(bot_id)


async def test_full_restore_into_empty_bot(stack):
    payload = await make_memory(stack)
    counts = await stack.seed.restore_full("bot2", payload)
    assert counts == {"persona": 3, "facts": 2, "events": 2, "insights": 1, "notes": 1}

    persona = await stack.db.fetchall(
        "SELECT layer, text FROM persona_entries WHERE bot_id='bot2' AND active=1 ORDER BY id"
    )
    assert [row["text"] for row in persona] == [
        text_of_len(60, "我骨子里"),
        text_of_len(60, "在人多的场合"),
        text_of_len(60, "我还没想明白"),
    ]
    facts = await stack.db.fetchall(
        "SELECT text, key_fact FROM facts WHERE bot_id='bot2' ORDER BY id"
    )
    assert {row["text"] for row in facts} == {"小明住在杭州", "答应过周末去看海"}
    assert {row["text"]: row["key_fact"] for row in facts} == {
        "小明住在杭州": 0,
        "答应过周末去看海": 1,
    }
    events = await stack.db.fetchall(
        "SELECT fragment, salience, storage_strength, status FROM events "
        "WHERE bot_id='bot2' ORDER BY id"
    )
    assert {row["fragment"] for row in events} == {
        "第一次见面是在雨天，她借了我一把伞",
        "周末一起去看海，风很大",
    }
    by_fragment = {row["fragment"]: row for row in events}
    assert by_fragment["第一次见面是在雨天，她借了我一把伞"]["salience"] == 3
    assert by_fragment["周末一起去看海，风很大"]["salience"] == 4
    assert all(row["status"] == "closed" for row in events)
    source_strength = None
    for row in payload["events"]:
        if row["fragment"].startswith("第一次见面"):
            source_strength = row["storage_strength"]
    assert by_fragment["第一次见面是在雨天，她借了我一把伞"][
        "storage_strength"
    ] == pytest.approx(source_strength)
    edges = await stack.db.scalar("SELECT COUNT(*) FROM event_edges WHERE bot_id='bot2'")
    assert int(edges or 0) > 0


async def test_restore_without_force_refuses_nonempty(stack):
    payload = await make_memory(stack)
    with pytest.raises(SeedError, match="force"):
        await stack.seed.restore_full("bot1", payload)


async def test_force_restore_backs_up_and_replaces(stack, workdir):
    payload = await make_memory(stack)
    before = await stack.db.scalar(
        "SELECT COUNT(*) FROM persona_entries WHERE bot_id='bot1'"
    )
    await stack.db.execute(
        "INSERT INTO persona_entries(bot_id, layer, text, source_event_ids, created_day, "
        "last_edited_day, active) VALUES('bot1','core','临时条目','[]',0,0,1)"
    )
    backup = workdir / "backup.db"
    counts = await stack.seed.restore_full(
        "bot1", payload, force=True, backup_path=backup
    )
    assert counts == {"persona": 3, "facts": 2, "events": 2, "insights": 1, "notes": 1}
    after = await stack.db.scalar(
        "SELECT COUNT(*) FROM persona_entries WHERE bot_id='bot1' AND active=1"
    )
    assert int(after or 0) == 3

    assert backup.is_file()
    conn = sqlite3.connect(str(backup))
    try:
        backed_up = conn.execute(
            "SELECT COUNT(*) FROM persona_entries WHERE bot_id='bot1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert int(backed_up) == int(before) + 1


async def test_insights_cleared_on_force_restore(stack):
    payload = await make_memory(stack)
    await stack.db.execute(
        "INSERT INTO insights(bot_id, text, source_event_ids, created_at) "
        "VALUES('bot1','旧洞察','[]',0)"
    )
    await stack.seed.restore_full("bot1", payload, force=True)
    rows = await stack.db.fetchall(
        "SELECT text FROM insights WHERE bot_id='bot1' ORDER BY id"
    )
    assert [row["text"] for row in rows] == ["海边那天的风很大"]


async def test_fact_source_event_id_is_remapped(stack):
    payload = await make_memory(stack)
    old_event = await stack.db.fetchone(
        "SELECT id FROM events WHERE bot_id='bot1' ORDER BY id LIMIT 1"
    )
    await stack.db.execute(
        "UPDATE facts SET source_event_id=? WHERE bot_id='bot1' AND text='小明住在杭州'",
        (int(old_event["id"]),),
    )
    payload = await stack.service.export_full("bot1")
    await stack.seed.restore_full("bot2", payload)
    fact = await stack.db.fetchone(
        "SELECT source_event_id FROM facts WHERE bot_id='bot2' AND text='小明住在杭州'"
    )
    assert fact["source_event_id"] is not None
    assert int(fact["source_event_id"]) != int(old_event["id"])
    mapped = await stack.db.fetchone(
        "SELECT bot_id FROM events WHERE id=?", (int(fact["source_event_id"]),)
    )
    assert mapped["bot_id"] == "bot2"


async def test_persona_source_ids_are_remapped(stack):
    payload = await make_memory(stack)
    old_event = await stack.db.fetchone(
        "SELECT id FROM events WHERE bot_id='bot1' ORDER BY id LIMIT 1"
    )
    await stack.db.execute(
        "UPDATE persona_entries SET source_event_ids=? WHERE bot_id='bot1' AND layer='core'",
        (json.dumps([int(old_event["id"])]),),
    )
    payload = await stack.service.export_full("bot1")
    await stack.seed.restore_full("bot2", payload)
    row = await stack.db.fetchone(
        "SELECT source_event_ids FROM persona_entries WHERE bot_id='bot2' AND layer='core'"
    )
    ids = json.loads(row["source_event_ids"])
    assert len(ids) == 1
    assert ids[0] != int(old_event["id"])
    mapped = await stack.db.fetchone("SELECT bot_id FROM events WHERE id=?", (ids[0],))
    assert mapped["bot_id"] == "bot2"


def test_validate_full_rejects_wrong_version():
    with pytest.raises(SeedError, match="full"):
        validate_full({"version": "persona_seed/v1", "persona": []})
    with pytest.raises(SeedError, match="JSON"):
        validate_full("not-a-dict")


def test_validate_full_normalizes_fields():
    normalized = validate_full(
        {
            "version": "shadow_memory/full/v1",
            "persona": [{"layer": "core", "text": "我在人群里", "source_event_ids": "[]"}],
            "facts": [{"kind": "weird", "text": "一条事实", "key_fact": 1}],
            "events": [
                {
                    "id": 7,
                    "fragment": "一次见面",
                    "entities": '["雨", "伞"]',
                    "salience": 99,
                    "storage_strength": "bad",
                }
            ],
        }
    )
    assert normalized["facts"][0]["kind"] == "world"
    assert normalized["facts"][0]["key_fact"] == 1
    assert normalized["events"][0]["old_id"] == 7
    assert normalized["events"][0]["entities"] == ["雨", "伞"]
    assert normalized["events"][0]["salience"] == 5
    assert normalized["events"][0]["storage_strength"] == pytest.approx(7.0)
