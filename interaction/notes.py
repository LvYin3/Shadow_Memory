"""笔记 / 日记：她自己的本子，永不进 prompt。"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.db import Database
from ..memory.textutil import bigrams, overlap


class NoteService:
    def __init__(self, db: Database, now_fn: Callable[[], int] = clock_mod.now_ms) -> None:
        self.db = db
        self.now_fn = now_fn

    async def write(
        self,
        bot_id: str,
        *,
        kind: str = "note",
        title: str = "",
        content: str = "",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        body = (content or "").strip()
        if not body:
            raise ValueError("内容不能为空")
        stamp = self.now_fn()
        entry_kind = "diary" if str(kind).lower() == "diary" else "note"
        result_title = (title or "").strip() or (
            clock_mod.day_key(stamp) if entry_kind == "diary" else body[:12]
        )
        note_id = await self.db.execute(
            "INSERT INTO notes(bot_id, kind, title, body, tags, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                bot_id,
                entry_kind,
                result_title[:60],
                body,
                json.dumps(tags or [], ensure_ascii=False),
                stamp,
                stamp,
            ),
        )
        return {"id": note_id, "kind": entry_kind, "title": result_title}

    async def read(self, bot_id: str, note_id: int) -> dict[str, Any] | None:
        return await self.db.fetchone(
            "SELECT * FROM notes WHERE id=? AND bot_id=?", (int(note_id), bot_id)
        )

    async def list(self, bot_id: str, kind: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        if kind in {"note", "diary"}:
            rows = await self.db.fetchall(
                "SELECT id, kind, title, body, tags, updated_at FROM notes WHERE bot_id=? AND kind=? "
                "ORDER BY id DESC LIMIT ?",
                (bot_id, kind, max(1, int(limit))),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT id, kind, title, body, tags, updated_at FROM notes WHERE bot_id=? "
                "ORDER BY id DESC LIMIT ?",
                (bot_id, max(1, int(limit))),
            )
        return rows

    async def search(
        self, bot_id: str, query: str, kind: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        tokens = bigrams(query)
        rows = await self.list(bot_id, kind, 200)
        scored = []
        for row in rows:
            text = f"{row.get('title') or ''} {row.get('body') or ''}"
            score = overlap(tokens, text)
            if score <= 0:
                continue
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        results = []
        for _, row in scored[: max(1, int(limit))]:
            results.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "title": row.get("title") or "",
                    "excerpt": (row.get("body") or "")[:60],
                }
            )
        return results

    async def stats(self, bot_id: str) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT kind, COUNT(*) AS n FROM notes WHERE bot_id=? GROUP BY kind", (bot_id,)
        )
        return {str(row["kind"]): int(row["n"]) for row in rows}
