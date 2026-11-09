"""会话目录：同一机器人接入过的会话，供备忘录跨会话投递。"""

from __future__ import annotations

from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.config import ConfigView
from ..core.db import Database

ConfigProvider = Callable[[str], ConfigView]


class SessionCatalog:
    def __init__(
        self,
        db: Database,
        configs: ConfigProvider,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.now_fn = now_fn

    async def list(self, bot_id: str) -> list[dict[str, Any]]:
        cfg = self.configs(bot_id)
        days = cfg.int("memo.catalog_days", 7, lo=1, hi=365)
        limit = cfg.int("memo.catalog_limit", 10, lo=1, hi=100)
        since = self.now_fn() - days * 86_400_000
        rows = await self.db.fetchall(
            "SELECT umo, name, type, last_message_at, last_summary FROM sessions "
            "WHERE bot_id=? AND last_message_at>=? ORDER BY last_message_at DESC LIMIT ?",
            (bot_id, since, limit),
        )
        return [dict(row) for row in rows]

    async def resolve(self, bot_id: str, umo: str) -> dict[str, Any] | None:
        return await self.db.fetchone(
            "SELECT umo, name, type, last_message_at, last_summary FROM sessions "
            "WHERE bot_id=? AND umo=?",
            (bot_id, umo),
        )

    async def contains(self, bot_id: str, umo: str) -> bool:
        cfg = self.configs(bot_id)
        days = cfg.int("memo.catalog_days", 7, lo=1, hi=365)
        since = self.now_fn() - days * 86_400_000
        row = await self.db.fetchone(
            "SELECT 1 FROM sessions WHERE bot_id=? AND umo=? AND last_message_at>=?",
            (bot_id, umo, since),
        )
        return row is not None

    async def render(self, bot_id: str) -> str:
        rows = await self.list(bot_id)
        if not rows:
            return "（没有可用的会话）"
        lines = []
        for row in rows:
            kind = "群" if str(row.get("type")) == "group" else "私聊"
            name = row.get("name") or row.get("umo")
            summary = (row.get("last_summary") or "")[:40]
            lines.append(f"- [{kind}] {name}（{row.get('umo')}）最后：{summary}")
        return "\n".join(lines)
