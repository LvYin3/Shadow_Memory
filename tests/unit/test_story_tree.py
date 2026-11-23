"""故事树、笔记工具，以及转移时把故事世界一起带走。"""

from __future__ import annotations

import json

import pytest

from astrbot_plugin_shadow_memory.story.cast import CastTools
from astrbot_plugin_shadow_memory.story.director import SceneProgress

from .story_helpers import BOT, CLOSING, OPENING, build_story, script_scene, start_story
from .test_transfer import env as transfer_env  # noqa: F401  复用转移夹具


async def test_bad_tree_keeps_the_previous_one(env):
    await start_story(env)
    previous = json.dumps(
        {"threads": [{"title": "旧线", "people": [], "canon": [], "seed": [], "scenes": []}], "daily": {"recent": [], "older": []}},
        ensure_ascii=False,
    )
    await env.store.update_state(BOT, tree=previous)
    closing = {**CLOSING, "tree": "不是树"}
    script_scene(env, closing=closing)
    await env.director.run(BOT)
    assert await env.db.scalar("SELECT COUNT(*) FROM story_scenes WHERE status='ok'") == 1
    assert json.loads((await env.store.state(BOT))["tree"])["threads"][0]["title"] == "旧线"


async def test_close_without_notes_still_commits_the_tree(env):
    await start_story(env)
    closing = {key: value for key, value in CLOSING.items() if key != "notes"}
    script_scene(env, closing=closing)
    await env.director.run(BOT)
    stored = json.loads((await env.store.state(BOT))["tree"])
    assert stored["threads"][0]["title"] == "暗线"
    assert stored["daily"]["recent"] == [] or stored["threads"][0]["scenes"]


async def test_note_tool_reads_everything_and_writes_once(env):
    await start_story(env)
    await env.store.apply_notes(BOT, [{"title": "旧提示", "body": "伞是别人的"}], None, 1)
    progress = SceneProgress(started_at=1, cause="manual")
    tools = CastTools(progress, [], cap=3, store=env.store, bot_id=BOT)
    text = await tools.run("read_story_notes", {})
    assert "旧提示" in text and "伞是别人的" in text
    first = await tools.run("write_story_note", {"title": "下一场", "body": "先去还书"})
    assert "记下了" in first
    second = await tools.run("write_story_note", {"title": "再一条", "body": "不要写"})
    assert "已经留过" in second
    rows = await env.store.notes(BOT)
    assert [row["title"] for row in rows] == ["下一场", "旧提示"] or {row["title"] for row in rows} == {
        "下一场",
        "旧提示",
    }
    assert "不要写" not in str(rows)


@pytest.fixture
def env(db, make_configs):
    return build_story(db, make_configs({"story": {"enabled": True}}))


async def test_v3_transfer_moves_the_story_world(transfer_env):
    db = transfer_env.stack.db
    now = 1_800_000_000_000
    event_id = await db.execute(
        "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, fragment, status, source, created_at) "
        "VALUES('bot1','','episodic',?,?,?,'closed','story',?)",
        (now, now, "她买了伞", now),
    )
    await db.execute(
        "INSERT INTO story_state(bot_id, enabled, paused, tokens, tokens_at, world_title, outline, "
        "tree, created_at, updated_at) VALUES('bot1',1,0,1,?,?,?,?,?,?)",
        (now, "青屿", "她是学生。", '{"threads":[{"title":"还书","people":[],"canon":[],"seed":[],"scenes":[1]}],"daily":{"recent":[],"older":[]}}', now, now),
    )
    scene_id = await db.execute(
        "INSERT INTO story_scenes(bot_id, cause, status, started_at, summary, event_id, created_at) "
        "VALUES('bot1','manual','ok',?,'她去还书',?,?)",
        (now, event_id, now),
    )
    await db.execute(
        "UPDATE story_state SET tree=? WHERE bot_id='bot1'",
        (
            json.dumps(
                {
                    "threads": [
                        {"title": "还书", "people": ["阿澄"], "canon": [], "seed": [], "scenes": [scene_id]}
                    ],
                    "daily": {"recent": [], "older": []},
                },
                ensure_ascii=False,
            ),
        ),
    )
    await db.execute(
        "INSERT INTO story_npcs(owner_bot_id, npc_bot_id, name, relationship, status, created_at, updated_at) "
        "VALUES('bot1','npc:bot1:1','阿澄','室友','active',?,?)",
        (now, now),
    )
    await db.execute(
        "INSERT INTO persona_entries(bot_id, layer, text, source_event_ids, created_day, last_edited_day, active) "
        "VALUES('npc:bot1:1','core','我是阿澄，画画的时候话很少。','[]',1,1,1)"
    )
    await db.execute(
        "INSERT INTO story_intents(bot_id, umo, display_name, body, status, created_at) "
        "VALUES('bot1','session-src','小明','想问伞的事','open',?)",
        (now,),
    )
    await db.execute(
        "INSERT INTO story_remotes(bot_id, umo, display_name, kind, claim, status, created_at) "
        "VALUES('bot1','session-src','小明','message','伞到了','open',?)",
        (now,),
    )
    await db.execute(
        "INSERT INTO story_unreads(bot_id, umo, display_name, text, created_at) VALUES('bot1','session-src','小明','源未回',?)",
        (now,),
    )
    await db.execute(
        "INSERT INTO story_sends(bot_id, umo, kind, text, created_at) VALUES('bot1','session-src','message','源发送',?)",
        (now,),
    )
    await db.execute(
        "INSERT INTO story_unreads(bot_id, umo, display_name, text, created_at) VALUES('bot2','keep','原目标','目标未回',?)",
        (now,),
    )
    await db.execute(
        "INSERT INTO story_state(bot_id, enabled, paused, tokens, tokens_at, outline, created_at, updated_at) "
        "VALUES('bot2',1,0,0,0,'目标原来的故事',?,?)",
        (now, now),
    )

    await transfer_env.service.execute("bot1", "bot2", ["events"], "replace", confirm_target="bot2")

    state = await db.fetchone("SELECT outline, tree FROM story_state WHERE bot_id='bot2'")
    assert state["outline"] == "她是学生。"
    tree = json.loads(state["tree"])
    new_scene = await db.fetchone("SELECT id, event_id, summary FROM story_scenes WHERE bot_id='bot2'")
    assert new_scene["summary"] == "她去还书"
    assert tree["threads"][0]["scenes"] == [new_scene["id"]]
    moved = await db.fetchone("SELECT fragment FROM events WHERE id=?", (new_scene["event_id"],))
    assert moved["fragment"] == "她买了伞"
    npc = await db.fetchone("SELECT npc_bot_id FROM story_npcs WHERE owner_bot_id='bot2'")
    assert npc["npc_bot_id"] == "npc:bot2:1"
    persona = await db.fetchone("SELECT text FROM persona_entries WHERE bot_id='npc:bot2:1'")
    assert "阿澄" in persona["text"]
    intent = await db.fetchone("SELECT umo, display_name FROM story_intents WHERE bot_id='bot2'")
    remote = await db.fetchone("SELECT umo FROM story_remotes WHERE bot_id='bot2'")
    assert intent["umo"] == "session-src" and intent["display_name"] == "小明"
    assert remote["umo"] == "session-src"
    assert await db.scalar("SELECT COUNT(*) FROM story_unreads WHERE bot_id='bot2' AND text='源未回'") == 0
    assert await db.scalar("SELECT COUNT(*) FROM story_unreads WHERE bot_id='bot2' AND text='目标未回'") == 1
    assert await db.scalar("SELECT COUNT(*) FROM story_sends WHERE bot_id='bot2'") == 0


async def test_v2_restore_does_not_touch_story(db, configs):
    from .helpers import build_stack

    stack = build_stack(db, configs)
    now = 1_800_000_000_000
    await stack.db.execute(
        "INSERT INTO story_state(bot_id, enabled, paused, tokens, tokens_at, outline, created_at, updated_at) "
        "VALUES('bot2',1,0,0,0,'原来的故事',?,?)",
        (now, now),
    )
    payload = {
        "version": "shadow_memory/full/v2",
        "bot_id": "bot1",
        "persona": [],
        "facts": [],
        "events": [],
        "insights": [],
        "notes": [],
        "story": {"state": {"outline": "不该写进来", "enabled": 1}},
    }
    await stack.seed.restore_full("bot2", payload, force=True)
    outline = await stack.db.scalar("SELECT outline FROM story_state WHERE bot_id='bot2'")
    assert outline == "原来的故事"
