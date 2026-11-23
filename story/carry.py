"""把一份故事世界整份搬走：状态、场、设定、笔记、树、配角和他们的记忆、意图、远方动作。

未回名单和发送记录不在里面。调用方已经开着事务，这里不再开一层。
"""

from __future__ import annotations

import json
from typing import Any

from . import tree


def rewrite_npc_id(npc_bot_id: str, source: str, target: str) -> str:
    prefix = f"npc:{source}:"
    text = str(npc_bot_id or "")
    if text.startswith(prefix):
        return f"npc:{target}:{text[len(prefix):]}"
    return text


def _remap_ids(raw: Any, id_map: dict[int, int]) -> str:
    try:
        values = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    except (TypeError, ValueError):
        return "[]"
    mapped: list[int] = []
    for value in values:
        try:
            old = int(value)
        except (TypeError, ValueError):
            continue
        if old in id_map:
            mapped.append(id_map[old])
    return json.dumps(mapped)


def _scene_id(value: Any, scene_map: dict[int, int]) -> int | None:
    try:
        old = int(value)
    except (TypeError, ValueError):
        return None
    return scene_map.get(old)


def _event_id(value: Any, event_map: dict[int, int]) -> int | None:
    try:
        old = int(value)
    except (TypeError, ValueError):
        return None
    if old not in event_map:
        return None
    return event_map[old]


def _rewrite_tree(raw: Any, scene_map: dict[int, int]) -> str:
    loaded = tree.loads(raw)
    threads: list[dict[str, Any]] = []
    for thread in loaded.get("threads") or []:
        if not isinstance(thread, dict):
            continue
        scenes = [
            scene_map[int(scene)]
            for scene in thread.get("scenes") or []
            if _scene_id(scene, scene_map) is not None
        ]
        threads.append({**thread, "scenes": scenes})
    daily = loaded.get("daily") if isinstance(loaded.get("daily"), dict) else {}
    recent = []
    for item in daily.get("recent") or []:
        if not isinstance(item, dict):
            continue
        new_id = _scene_id(item.get("id"), scene_map)
        if new_id is None:
            continue
        recent.append({**item, "id": new_id})
    older = [
        scene_map[int(scene)]
        for scene in daily.get("older") or []
        if _scene_id(scene, scene_map) is not None
    ]
    return json.dumps({"threads": threads, "daily": {"recent": recent, "older": older}}, ensure_ascii=False)


async def export_world(db: Any, bot_id: str) -> dict[str, Any]:
    state = await db.fetchone("SELECT * FROM story_state WHERE bot_id=?", (bot_id,))
    scenes = await db.fetchall(
        "SELECT * FROM story_scenes WHERE bot_id=? ORDER BY id ASC", (bot_id,)
    )
    canon = await db.fetchall(
        "SELECT * FROM story_canon WHERE bot_id=? ORDER BY id ASC", (bot_id,)
    )
    notes = await db.fetchall(
        "SELECT * FROM story_notes WHERE bot_id=? ORDER BY id ASC", (bot_id,)
    )
    npcs = await db.fetchall(
        "SELECT * FROM story_npcs WHERE owner_bot_id=? ORDER BY id ASC", (bot_id,)
    )
    intents = await db.fetchall(
        "SELECT * FROM story_intents WHERE bot_id=? ORDER BY id ASC", (bot_id,)
    )
    remotes = await db.fetchall(
        "SELECT * FROM story_remotes WHERE bot_id=? ORDER BY id ASC", (bot_id,)
    )
    memories: list[dict[str, Any]] = []
    for npc in npcs:
        npc_id = str(npc["npc_bot_id"])
        memories.append(
            {
                "npc_bot_id": npc_id,
                "persona": await db.fetchall(
                    "SELECT layer, text, source_event_ids FROM persona_entries "
                    "WHERE bot_id=? AND active=1",
                    (npc_id,),
                ),
                "facts": await db.fetchall("SELECT * FROM facts WHERE bot_id=?", (npc_id,)),
                "events": await db.fetchall(
                    "SELECT * FROM events WHERE bot_id=? ORDER BY id ASC", (npc_id,)
                ),
                "insights": await db.fetchall(
                    "SELECT text, source_event_ids, created_at FROM insights WHERE bot_id=?",
                    (npc_id,),
                ),
            }
        )
    return {
        "state": dict(state) if state else None,
        "scenes": [dict(row) for row in scenes],
        "canon": [dict(row) for row in canon],
        "notes": [dict(row) for row in notes],
        "npcs": [dict(row) for row in npcs],
        "intents": [dict(row) for row in intents],
        "remotes": [dict(row) for row in remotes],
        "npc_memory": memories,
    }


def has_world(world: dict[str, Any] | None) -> bool:
    if not isinstance(world, dict):
        return False
    if world.get("state"):
        return True
    return any(world.get(key) for key in ("scenes", "canon", "notes", "npcs", "intents", "remotes"))


async def replace_world(
    db: Any,
    target: str,
    world: dict[str, Any] | None,
    *,
    source: str,
    event_id_map: dict[int, int],
    now: int,
) -> None:
    """清掉目标的故事世界、意图和远方动作，再写入源的那一份。未回和发送记录不动。"""
    old_npcs = await db.fetchall(
        "SELECT npc_bot_id FROM story_npcs WHERE owner_bot_id=?", (target,)
    )
    for row in old_npcs:
        npc_id = str(row["npc_bot_id"])
        await db.execute("DELETE FROM events WHERE bot_id=?", (npc_id,))
        await db.execute("DELETE FROM facts WHERE bot_id=?", (npc_id,))
        await db.execute("DELETE FROM insights WHERE bot_id=?", (npc_id,))
        await db.execute("DELETE FROM persona_entries WHERE bot_id=?", (npc_id,))
    for table in (
        "story_scenes",
        "story_canon",
        "story_notes",
        "story_intents",
        "story_remotes",
        "story_npcs",
        "story_state",
    ):
        column = "owner_bot_id" if table == "story_npcs" else "bot_id"
        await db.execute(f"DELETE FROM {table} WHERE {column}=?", (target,))

    if not has_world(world):
        return
    assert world is not None
    scene_map: dict[int, int] = {}
    for row in world.get("scenes") or []:
        if not isinstance(row, dict):
            continue
        new_id = await db.execute(
            "INSERT INTO story_scenes(bot_id, cause, status, started_at, ended_at, rounds, calls, "
            "seed, rolls, transcript, backfill, summary, salience, event_id, backfill_event_id, "
            "error, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                target,
                row.get("cause") or "manual",
                row.get("status") or "ok",
                int(row.get("started_at") or now),
                row.get("ended_at"),
                int(row.get("rounds") or 0),
                int(row.get("calls") or 0),
                row.get("seed"),
                row.get("rolls"),
                row.get("transcript"),
                row.get("backfill"),
                row.get("summary"),
                row.get("salience"),
                _event_id(row.get("event_id"), event_id_map),
                _event_id(row.get("backfill_event_id"), event_id_map),
                row.get("error"),
                int(row.get("created_at") or now),
            ),
        )
        try:
            scene_map[int(row["id"])] = int(new_id)
        except (KeyError, TypeError, ValueError):
            pass

    state = world.get("state") if isinstance(world.get("state"), dict) else None
    if state:
        await db.execute(
            "INSERT INTO story_state(bot_id, enabled, paused, pause_reason, tokens, tokens_at, "
            "card, next_wake_at, last_scene_at, failures, world_title, outline, seed_meta, tree, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                target,
                int(state.get("enabled") or 0),
                int(state.get("paused") or 0),
                state.get("pause_reason"),
                float(state.get("tokens") or 0.0),
                int(state.get("tokens_at") or 0),
                state.get("card"),
                state.get("next_wake_at"),
                state.get("last_scene_at"),
                int(state.get("failures") or 0),
                state.get("world_title"),
                state.get("outline"),
                state.get("seed_meta"),
                _rewrite_tree(state.get("tree"), scene_map),
                int(state.get("created_at") or now),
                int(state.get("updated_at") or now),
            ),
        )

    for row in world.get("canon") or []:
        if not isinstance(row, dict):
            continue
        await db.execute(
            "INSERT INTO story_canon(bot_id, origin, category, name, body, scene_id, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                target,
                row.get("origin") if row.get("origin") in {"seed", "play"} else "play",
                row.get("category") or "other",
                row.get("name") or "",
                row.get("body") or "",
                _scene_id(row.get("scene_id"), scene_map),
                int(row.get("created_at") or now),
            ),
        )
    for row in world.get("notes") or []:
        if not isinstance(row, dict) or not str(row.get("title") or "").strip():
            continue
        await db.execute(
            "INSERT INTO story_notes(bot_id, title, body, scene_id, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                target,
                row.get("title"),
                row.get("body") or "",
                _scene_id(row.get("scene_id"), scene_map),
                int(row.get("created_at") or now),
                int(row.get("updated_at") or now),
            ),
        )
    npc_ids: dict[str, str] = {}
    for row in world.get("npcs") or []:
        if not isinstance(row, dict):
            continue
        new_npc = rewrite_npc_id(str(row.get("npc_bot_id") or ""), source, target)
        npc_ids[str(row.get("npc_bot_id") or "")] = new_npc
        await db.execute(
            "INSERT INTO story_npcs(owner_bot_id, npc_bot_id, name, relationship, status, "
            "last_seen_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                target,
                new_npc,
                row.get("name") or "",
                row.get("relationship") or "",
                row.get("status") or "active",
                row.get("last_seen_at"),
                int(row.get("created_at") or now),
                int(row.get("updated_at") or now),
            ),
        )
    for row in world.get("intents") or []:
        if not isinstance(row, dict):
            continue
        await db.execute(
            "INSERT INTO story_intents(bot_id, umo, display_name, body, status, scene_id, "
            "created_at, settled_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                target,
                row.get("umo") or "",
                row.get("display_name") or "",
                row.get("body") or "",
                row.get("status") or "open",
                _scene_id(row.get("scene_id"), scene_map),
                int(row.get("created_at") or now),
                row.get("settled_at"),
            ),
        )
    for row in world.get("remotes") or []:
        if not isinstance(row, dict):
            continue
        await db.execute(
            "INSERT INTO story_remotes(bot_id, umo, display_name, kind, claim, status, scene_id, "
            "created_at, resolved_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                target,
                row.get("umo") or "",
                row.get("display_name") or "",
                row.get("kind") or "",
                row.get("claim") or "",
                row.get("status") or "open",
                _scene_id(row.get("scene_id"), scene_map),
                int(row.get("created_at") or now),
                row.get("resolved_at"),
            ),
        )
    for memory in world.get("npc_memory") or []:
        if not isinstance(memory, dict):
            continue
        npc_id = npc_ids.get(str(memory.get("npc_bot_id") or ""))
        if not npc_id:
            npc_id = rewrite_npc_id(str(memory.get("npc_bot_id") or ""), source, target)
        await _copy_npc_memory(db, npc_id, memory, now)


async def _copy_npc_memory(db: Any, npc_id: str, memory: dict[str, Any], now: int) -> None:
    id_map: dict[int, int] = {}
    for row in memory.get("events") or []:
        if not isinstance(row, dict):
            continue
        new_id = await db.execute(
            "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, participants, "
            "turn_count, char_count, fragment, summary, entities, source_event_ids, "
            "storage_strength, retrievability, last_recalled_at, salience, status, source, "
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?)",
            (
                npc_id,
                "",
                row.get("kind") or "episodic",
                int(row.get("started_at") or row.get("ended_at") or now),
                row.get("ended_at"),
                row.get("participants") if isinstance(row.get("participants"), str) else "[]",
                int(row.get("turn_count") or 0),
                int(row.get("char_count") or len(str(row.get("fragment") or ""))),
                row.get("fragment") or "",
                row.get("summary"),
                row.get("entities") if isinstance(row.get("entities"), str) else "[]",
                float(row.get("storage_strength") or 7.0),
                float(row.get("retrievability") or 1.0),
                row.get("last_recalled_at"),
                row.get("salience"),
                row.get("status") or "closed",
                "story" if str(row.get("source") or "") == "story" else "chat",
                int(row.get("created_at") or now),
            ),
        )
        try:
            id_map[int(row["id"])] = int(new_id)
        except (KeyError, TypeError, ValueError):
            pass
    for row in memory.get("facts") or []:
        if not isinstance(row, dict) or not str(row.get("text") or "").strip():
            continue
        source_event = _event_id(row.get("source_event_id"), id_map)
        await db.execute(
            "INSERT INTO facts(bot_id, kind, subject_id, text, key_fact, storage_strength, "
            "retrievability, last_recalled_at, valid_from, valid_to, superseded_by, archived, "
            "source_event_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                npc_id,
                row.get("kind") or "world",
                row.get("subject_id"),
                row.get("text"),
                int(row.get("key_fact") or 0),
                float(row.get("storage_strength") or 30.0),
                float(row.get("retrievability") or 1.0),
                row.get("last_recalled_at"),
                row.get("valid_from") or now,
                row.get("valid_to"),
                row.get("superseded_by"),
                int(row.get("archived") or 0),
                source_event,
                int(row.get("created_at") or now),
            ),
        )
    for row in memory.get("persona") or []:
        if not isinstance(row, dict) or not str(row.get("text") or "").strip():
            continue
        await db.execute(
            "INSERT INTO persona_entries(bot_id, layer, text, source_event_ids, created_day, "
            "last_edited_day, active) VALUES(?,?,?,?,?,?,1)",
            (
                npc_id,
                row.get("layer") or "core",
                row.get("text"),
                _remap_ids(row.get("source_event_ids"), id_map),
                int(now // 86_400_000),
                int(now // 86_400_000),
            ),
        )
    for row in memory.get("insights") or []:
        if not isinstance(row, dict) or not str(row.get("text") or "").strip():
            continue
        await db.execute(
            "INSERT INTO insights(bot_id, text, source_event_ids, used_in_version, created_at) "
            "VALUES(?,?,?,NULL,?)",
            (
                npc_id,
                row.get("text"),
                _remap_ids(row.get("source_event_ids"), id_map),
                int(row.get("created_at") or now),
            ),
        )
