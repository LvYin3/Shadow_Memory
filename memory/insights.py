"""洞察仓储：巩固产物，只喂蒸馏，不进召回。"""

from __future__ import annotations

import json
from typing import Any

from ..core.db import Database


class InsightRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, bot_id: str, text: str, source_event_ids: list[int], now: int) -> int:
        return await self.db.execute(
            "INSERT INTO insights(bot_id, text, source_event_ids, used_in_version, created_at) "
            "VALUES(?,?,?,NULL,?)",
            (
                bot_id,
                text[:160],
                json.dumps(sorted({int(value) for value in source_event_ids}), ensure_ascii=False),
                now,
            ),
        )

    async def unused(self, bot_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            "SELECT * FROM insights WHERE bot_id=? AND used_in_version IS NULL "
            "ORDER BY id ASC LIMIT ?",
            (bot_id, max(1, int(limit))),
        )

    async def mark_used(self, bot_id: str, version: int, insight_ids: list[int]) -> None:
        if not insight_ids:
            return
        placeholders = ",".join("?" for _ in insight_ids)
        await self.db.execute(
            f"UPDATE insights SET used_in_version=? WHERE bot_id=? AND id IN ({placeholders})",
            (int(version), bot_id, *[int(value) for value in insight_ids]),
        )

    async def count_unused(self, bot_id: str) -> int:
        value = await self.db.scalar(
            "SELECT COUNT(*) FROM insights WHERE bot_id=? AND used_in_version IS NULL",
            (bot_id,),
            default=0,
        )
        return int(value or 0)
