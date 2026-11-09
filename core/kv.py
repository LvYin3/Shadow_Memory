"""按机器人存放的小型键值配置（WebUI 写入，运行时读取）。"""

from __future__ import annotations

from typing import Any

from . import clock as clock_mod
from .db import Database


class KvStore:
    def __init__(self, db: Database, now_fn=clock_mod.now_ms) -> None:
        self.db = db
        self.now_fn = now_fn

    async def get(self, bot_id: str, key: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT value FROM kv WHERE bot_id=? AND key=?", (bot_id, key)
        )
        return None if row is None else row.get("value")

    async def set(self, bot_id: str, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO kv(bot_id, key, value, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(bot_id, key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (bot_id, key, str(value), self.now_fn()),
        )

    async def delete(self, bot_id: str, key: str) -> None:
        await self.db.execute("DELETE FROM kv WHERE bot_id=? AND key=?", (bot_id, key))

    async def mapping(self, bot_id: str, prefix: str = "") -> dict[str, Any]:
        rows = await self.db.fetchall(
            "SELECT key, value FROM kv WHERE bot_id=? AND key LIKE ? ORDER BY key",
            (bot_id, f"{prefix}%"),
        )
        return {str(row["key"]): row.get("value") for row in rows}
