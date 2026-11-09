from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_shadow_memory.interaction.media import MediaService
from astrbot_plugin_shadow_memory.interaction.media_dir import MediaDir
from astrbot_plugin_shadow_memory.interaction.media_transfer import MediaTransfer
from astrbot_plugin_shadow_memory.memory.transfer import TransferError, TransferService

from ..fakes import FakeHTTP
from .helpers import build_stack, text_of_len

NOW = 1_800_000_000_000


@pytest.fixture
def env(db, configs, workdir):
    stack = build_stack(db, configs)
    root = workdir / "media"

    def dir_provider(bot_id: str) -> MediaDir:
        return MediaDir(root, bot_id)

    media = MediaService(db, configs, dir_provider, FakeHTTP(), stack.audit)
    transfer = MediaTransfer(db, media, workdir, now_fn=lambda: NOW)
    service = TransferService(
        db,
        configs,
        stack.persona,
        stack.edges,
        transfer,
        stack.audit,
        now_fn=lambda: NOW,
    )
    return SimpleNamespace(
        stack=stack, media=media, transfer=transfer, service=service, root=root
    )


def seed_payload():
    return {
        "version": "persona_seed/v1",
        "bot_hint": "bot1",
        "persona": {
            "core": [text_of_len(60, "我骨子里")],
            "edge": [text_of_len(60, "在人多的场合")],
            "tension": [],
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
                "participants": [],
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


async def seed_bot(env, bot_id="bot1"):
    await env.stack.seed.import_seed(bot_id, seed_payload(), force=True)


async def add_media(env, bot_id="bot1", filename="a.png", data=b"PNG-A"):
    folder = env.root / bot_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(data)
    await env.stack.db.execute(
        "INSERT INTO media_items(bot_id, filename, description, tags, source, source_ref, "
        "sender_id, sender_name, source_umo, size, created_at, last_used_at, use_count) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            bot_id,
            filename,
            "一张图",
            "[]",
            "manual",
            "",
            "u1",
            "小明",
            "umo1",
            len(data),
            0,
            0,
            0,
        ),
    )


async def test_preview_counts(env):
    await seed_bot(env)
    await add_media(env)
    result = await env.service.preview("bot1", "bot2", ["persona", "facts", "events", "media"], "merge")
    assert result["counts"]["persona"] == 2
    assert result["counts"]["facts"] == 2
    assert result["counts"]["events"] == 2
    assert result["media"]["files"] == 1


async def test_merge_copies_and_is_idempotent(env):
    await seed_bot(env)
    await add_media(env)
    first = await env.service.execute(
        "bot1", "bot2", ["persona", "facts", "events", "media"], "merge"
    )
    assert first["counts"] == {
        "persona": 2,
        "facts": 2,
        "events": 2,
        "insights": 0,
        "notes": 0,
        "media": 1,
    }
    assert await env.stack.db.scalar("SELECT COUNT(*) FROM events WHERE bot_id='bot1'") == 2
    assert await env.stack.db.scalar("SELECT COUNT(*) FROM events WHERE bot_id='bot2'") == 2
    assert (env.root / "bot2" / "a.png").is_file()

    second = await env.service.execute(
        "bot1", "bot2", ["persona", "facts", "events", "media"], "merge"
    )
    assert second["counts"] == {
        "persona": 0,
        "facts": 0,
        "events": 0,
        "insights": 0,
        "notes": 0,
        "media": 0,
    }
    assert (await env.transfer.preview("bot1", "bot2"))["files"] == 1


async def test_replace_clears_target_and_moves_media_to_trash(env):
    await seed_bot(env, "bot1")
    await add_media(env, "bot1", "a.png", b"NEW")
    await seed_bot(env, "bot2")
    await add_media(env, "bot2", "a.png", b"OLD")
    await env.stack.db.execute(
        "INSERT INTO notes(bot_id, kind, title, body, tags, created_at, updated_at) "
        "VALUES('bot2','note','旧笔记','要清掉','[]',0,0)"
    )
    with pytest.raises(TransferError, match="目标"):
        await env.service.execute("bot1", "bot2", ["persona", "facts", "events", "notes", "media"], "replace")
    result = await env.service.execute(
        "bot1",
        "bot2",
        ["persona", "facts", "events", "notes", "media"],
        "replace",
        confirm_target="bot2",
    )
    assert result["counts"]["events"] == 2
    assert await env.stack.db.scalar("SELECT COUNT(*) FROM notes WHERE bot_id='bot2'") == 0
    assert (env.root / "bot2" / "a.png").read_bytes() == b"NEW"
    assert result["trash"]
    assert list((env.root.parent / "exports").glob("trash_*"))


async def test_same_bot_rejected(env):
    with pytest.raises(TransferError):
        await env.service.preview("bot1", "bot1", ["persona"], "merge")


async def test_media_collision_renames(env):
    await seed_bot(env, "bot1")
    await add_media(env, "bot1", "a.png", b"NEW")
    await add_media(env, "bot2", "a.png", b"OLD")
    result = await env.service.execute("bot1", "bot2", ["media"], "merge")
    assert result["media"]["renamed"] == 1
    assert (env.root / "bot2" / "a_2.png").read_bytes() == b"NEW"


async def test_media_cap_rejects(env, monkeypatch):
    await seed_bot(env, "bot1")
    await add_media(env, "bot1", "a.png", b"1")
    await add_media(env, "bot1", "b.png", b"2")
    monkeypatch.setattr(
        "astrbot_plugin_shadow_memory.memory.transfer.MAX_MEDIA_FILES", 1
    )
    with pytest.raises(TransferError, match="媒体"):
        await env.service.execute("bot1", "bot2", ["media"], "merge")


async def test_fact_source_event_id_remapped(env):
    await seed_bot(env)
    old = await env.stack.db.fetchone(
        "SELECT id FROM events WHERE bot_id='bot1' ORDER BY id LIMIT 1"
    )
    await env.stack.db.execute(
        "UPDATE facts SET source_event_id=? WHERE bot_id='bot1' AND text='小明住在杭州'",
        (int(old["id"]),),
    )
    await env.service.execute("bot1", "bot2", ["events", "facts"], "merge")
    fact = await env.stack.db.fetchone(
        "SELECT source_event_id FROM facts WHERE bot_id='bot2' AND text='小明住在杭州'"
    )
    assert fact["source_event_id"] is not None
    assert int(fact["source_event_id"]) != int(old["id"])
    source = await env.stack.db.fetchone(
        "SELECT bot_id FROM events WHERE id=?", (int(fact["source_event_id"]),)
    )
    assert source["bot_id"] == "bot2"
