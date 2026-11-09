"""每个会话最近消息的环形缓冲，供固定上下文注入与 memo 唤醒使用。"""

from __future__ import annotations

from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.db import Database

ROLES = {"user", "assistant"}


class SessionBuffer:
    def __init__(
        self,
        db: Database,
        now_fn: Callable[[], int] = clock_mod.now_ms,
        *,
        keep: int = 200,
    ) -> None:
        self.db = db
        self.now_fn = now_fn
        self.keep = max(20, int(keep))

    async def record(
        self,
        bot_id: str,
        umo: str,
        role: str,
        text: str,
        speaker: str = "",
        ts: int | None = None,
    ) -> None:
        body = (text or "").strip()
        if not body or role not in ROLES:
            return
        await self.db.execute(
            "INSERT INTO session_messages(bot_id, umo, role, speaker, text, ts) "
            "VALUES(?,?,?,?,?,?)",
            (bot_id, umo, role, (speaker or "")[:64] or None, body[:4000], ts or self.now_fn()),
        )
        await self.db.execute(
            "DELETE FROM session_messages WHERE bot_id=? AND umo=? AND id <= ("
            " SELECT id FROM session_messages WHERE bot_id=? AND umo=? "
            " ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (bot_id, umo, bot_id, umo, self.keep),
        )

    async def last(self, bot_id: str, umo: str, limit: int = 20) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        rows = await self.db.fetchall(
            "SELECT role, speaker, text, ts FROM session_messages "
            "WHERE bot_id=? AND umo=? ORDER BY id DESC LIMIT ?",
            (bot_id, umo, int(limit)),
        )
        return [dict(row) for row in reversed(rows)]

    async def count(self, bot_id: str, umo: str) -> int:
        value = await self.db.scalar(
            "SELECT COUNT(*) FROM session_messages WHERE bot_id=? AND umo=?",
            (bot_id, umo),
            default=0,
        )
        return int(value or 0)

    async def clear(self, bot_id: str, umo: str) -> None:
        await self.db.execute(
            "DELETE FROM session_messages WHERE bot_id=? AND umo=?", (bot_id, umo)
        )
