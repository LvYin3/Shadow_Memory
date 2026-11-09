"""实体归一化、建边、邻居查询。"""

from __future__ import annotations

import unicodedata
from typing import Any, Iterable, Mapping

from ..core.db import Database

MAX_ENTITIES = 8
MAX_ENTITY_CHARS = 32


def _clean_entity(text: Any) -> str:
    if text is None:
        return ""
    raw = str(text).strip().lower()
    kept: list[str] = []
    for char in raw:
        if char.isspace():
            continue
        category = unicodedata.category(char)
        if category[0] in {"P", "S", "Z"}:
            continue
        kept.append(char)
    return "".join(kept)[:MAX_ENTITY_CHARS]


def normalize_entities(values: Iterable[Any] | None) -> list[str]:
    """小写、去标点/空白、1–32 字、去重、最多 8 个。"""
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    result: list[str] = []
    for value in values:
        cleaned = _clean_entity(value)
        if not cleaned or cleaned in result:
            continue
        result.append(cleaned)
        if len(result) >= MAX_ENTITIES:
            break
    return result


def entities_from_json(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        import json

        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return []
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
    return []


class EdgeRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def link(
        self,
        bot_id: str,
        event_id: int,
        entities: Iterable[Any] | None,
        now: int,
        max_degree: int = 20,
    ) -> int:
        names = normalize_entities(entities)
        if not names:
            return 0
        entity_ids: list[int] = []
        for name in names:
            await self.db.execute(
                "INSERT OR IGNORE INTO entities(bot_id, name, canonical, created_at) "
                "VALUES(?,?,?,?)",
                (bot_id, name, name, now),
            )
            row = await self.db.fetchone(
                "SELECT id FROM entities WHERE bot_id=? AND canonical=?", (bot_id, name)
            )
            if row:
                entity_ids.append(int(row["id"]))
        if not entity_ids:
            return 0
        for entity_id in entity_ids:
            await self.db.execute(
                "INSERT OR IGNORE INTO event_entities(event_id, entity_id) VALUES(?,?)",
                (event_id, entity_id),
            )
        placeholders = ",".join("?" for _ in entity_ids)
        rows = await self.db.fetchall(
            f"""
            SELECT ee2.event_id AS other, COUNT(*) AS shared
            FROM event_entities ee1
            JOIN event_entities ee2 ON ee1.entity_id = ee2.entity_id
            WHERE ee1.event_id = ? AND ee2.event_id <> ?
              AND ee2.entity_id IN ({placeholders})
            GROUP BY ee2.event_id
            """,
            (event_id, event_id, *entity_ids),
        )
        touched = 0
        for row in rows:
            other = int(row["other"])
            weight = max(1, int(row["shared"]))
            src, dst = (event_id, other) if event_id < other else (other, event_id)
            await self.db.execute(
                """
                INSERT INTO event_edges(bot_id, src_event_id, dst_event_id, weight, last_seen_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(bot_id, src_event_id, dst_event_id) DO UPDATE SET
                  weight = weight + excluded.weight,
                  last_seen_at = excluded.last_seen_at
                """,
                (bot_id, src, dst, weight, now),
            )
            touched += 1
        await self.prune(bot_id, event_id, max_degree)
        return touched

    async def neighbors(self, bot_id: str, event_id: int, limit: int = 4) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            """
            SELECT CASE WHEN src_event_id = ? THEN dst_event_id ELSE src_event_id END AS event_id,
                   weight, last_seen_at
            FROM event_edges
            WHERE bot_id = ? AND (src_event_id = ? OR dst_event_id = ?)
            ORDER BY weight DESC, last_seen_at DESC
            LIMIT ?
            """,
            (event_id, bot_id, event_id, event_id, max(0, int(limit))),
        )

    async def degree(self, bot_id: str, event_ids: Iterable[int]) -> dict[int, int]:
        result: dict[int, int] = {}
        for event_id in event_ids:
            count = await self.db.scalar(
                "SELECT COUNT(*) FROM event_edges WHERE bot_id=? AND (src_event_id=? OR dst_event_id=?)",
                (bot_id, int(event_id), int(event_id)),
                default=0,
            )
            result[int(event_id)] = int(count or 0)
        return result

    async def prune(self, bot_id: str, event_id: int, max_degree: int = 20) -> None:
        keep = max(0, int(max_degree))
        rows = await self.db.fetchall(
            """
            SELECT id FROM event_edges
            WHERE bot_id=? AND (src_event_id=? OR dst_event_id=?)
            ORDER BY weight DESC, last_seen_at DESC
            """,
            (bot_id, event_id, event_id),
        )
        if len(rows) <= keep:
            return
        stale = [int(row["id"]) for row in rows[keep:]]
        if not stale:
            return
        placeholders = ",".join("?" for _ in stale)
        await self.db.execute(
            f"DELETE FROM event_edges WHERE id IN ({placeholders})", tuple(stale)
        )

    async def stats(self, bot_id: str) -> Mapping[str, Any]:
        edges = await self.db.scalar(
            "SELECT COUNT(*) FROM event_edges WHERE bot_id=?", (bot_id,), default=0
        )
        entities = await self.db.scalar(
            "SELECT COUNT(*) FROM entities WHERE bot_id=?", (bot_id,), default=0
        )
        return {"edges": int(edges or 0), "entities": int(entities or 0)}
