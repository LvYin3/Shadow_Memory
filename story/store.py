"""故事各表的读写：状态、世界种子与设定集、主持人笔记、场景记录。"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.db import Database

STORY_UMO_PREFIX = "story:"

_STATE_COLUMNS = {
    "enabled",
    "paused",
    "pause_reason",
    "tokens",
    "tokens_at",
    "card",
    "next_wake_at",
    "last_scene_at",
    "failures",
    "world_title",
    "outline",
    "seed_meta",
}


def story_umo(bot_id: str) -> str:
    """故事事件挂的专用会话；它不对应任何 QQ 会话，也不会进会话目录。"""
    return f"{STORY_UMO_PREFIX}{bot_id}"


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default
    return default


class StoryStore:
    def __init__(self, db: Database, now_fn: Callable[[], int] = clock_mod.now_ms) -> None:
        self.db = db
        self.now_fn = now_fn

    # ------------------------------------------------------------------ 状态
    async def state(self, bot_id: str) -> dict[str, Any] | None:
        return await self.db.fetchone("SELECT * FROM story_state WHERE bot_id=?", (bot_id,))

    async def ensure_state(self, bot_id: str, now: int | None = None) -> dict[str, Any]:
        stamp = now or self.now_fn()
        await self.db.execute(
            "INSERT OR IGNORE INTO story_state(bot_id, created_at, updated_at) VALUES(?,?,?)",
            (bot_id, stamp, stamp),
        )
        row = await self.state(bot_id)
        assert row is not None
        return row

    async def update_state(self, bot_id: str, now: int | None = None, **fields: Any) -> None:
        unknown = set(fields) - _STATE_COLUMNS
        if unknown:
            raise ValueError(f"unknown story_state columns: {sorted(unknown)}")
        stamp = now or self.now_fn()
        columns = list(fields)
        assignments = ", ".join(f"{column}=?" for column in columns)
        await self.db.execute(
            f"UPDATE story_state SET {assignments}{', ' if columns else ''}updated_at=? "
            "WHERE bot_id=?",
            (*[fields[column] for column in columns], stamp, bot_id),
        )

    async def due(self, now: int) -> list[str]:
        rows = await self.db.fetchall(
            "SELECT bot_id FROM story_state WHERE enabled=1 AND paused=0 "
            "AND next_wake_at IS NOT NULL AND next_wake_at<=? ORDER BY next_wake_at",
            (now,),
        )
        return [str(row["bot_id"]) for row in rows]

    async def bot_ids(self) -> list[str]:
        rows = await self.db.fetchall("SELECT bot_id FROM story_state ORDER BY bot_id")
        return [str(row["bot_id"]) for row in rows]

    @staticmethod
    def seed_meta(state: dict[str, Any] | None) -> dict[str, Any]:
        meta = _loads((state or {}).get("seed_meta"), {})
        return meta if isinstance(meta, dict) else {}

    # ------------------------------------------------------------------ 世界种子
    async def replace_seed(
        self, bot_id: str, seed: dict[str, Any], now: int | None = None
    ) -> dict[str, int]:
        """导入（或重新导入）世界种子：换掉大纲、钩子和种子条目，演出中新增的设定不动。"""
        stamp = now or self.now_fn()
        entries = list(seed.get("entries") or [])
        meta = {
            "version": seed.get("version"),
            "hooks": list(seed.get("hooks") or []),
            "opening": dict(seed.get("opening") or {}),
            "imported_at": stamp,
        }
        async with self.db.transaction():
            await self.ensure_state(bot_id, stamp)
            await self.update_state(
                bot_id,
                stamp,
                world_title=str(seed.get("title") or ""),
                outline=str(seed.get("outline") or ""),
                seed_meta=_dumps(meta),
            )
            await self.db.execute(
                "DELETE FROM story_canon WHERE bot_id=? AND origin='seed'", (bot_id,)
            )
            for entry in entries:
                await self.db.execute(
                    "INSERT INTO story_canon(bot_id, origin, category, name, body, scene_id, "
                    "created_at) VALUES(?,?,?,?,?,NULL,?)",
                    (bot_id, "seed", entry["category"], entry["name"], entry["body"], stamp),
                )
        return {"entries": len(entries), "hooks": len(meta["hooks"])}

    # ------------------------------------------------------------------ 设定集
    async def canon(self, bot_id: str) -> list[dict[str, Any]]:
        """种子条目在前，演出中新增的在后，各按登记顺序。"""
        return await self.db.fetchall(
            "SELECT id, origin, category, name, body, scene_id, created_at FROM story_canon "
            "WHERE bot_id=? ORDER BY CASE origin WHEN 'seed' THEN 0 ELSE 1 END, id",
            (bot_id,),
        )

    async def canon_counts(self, bot_id: str) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT origin, COUNT(*) AS n FROM story_canon WHERE bot_id=? GROUP BY origin",
            (bot_id,),
        )
        counts = {"seed": 0, "play": 0}
        for row in rows:
            counts[str(row["origin"])] = int(row["n"] or 0)
        return counts

    async def add_canon(
        self, bot_id: str, items: list[dict[str, str]], scene_id: int | None, now: int
    ) -> int:
        """只追加：同名同内容的不重复登记，种子条目永远不改。"""
        added = 0
        for item in items:
            exists = await self.db.fetchone(
                "SELECT 1 FROM story_canon WHERE bot_id=? AND name=? AND body=?",
                (bot_id, item["name"], item["body"]),
            )
            if exists:
                continue
            await self.db.execute(
                "INSERT INTO story_canon(bot_id, origin, category, name, body, scene_id, "
                "created_at) VALUES(?,?,?,?,?,?,?)",
                (bot_id, "play", item["category"], item["name"], item["body"], scene_id, now),
            )
            added += 1
        return added

    # ------------------------------------------------------------------ 主持人笔记
    async def notes(self, bot_id: str) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            "SELECT id, title, body, scene_id, created_at, updated_at FROM story_notes "
            "WHERE bot_id=? ORDER BY updated_at DESC, id DESC",
            (bot_id,),
        )

    async def apply_notes(
        self, bot_id: str, items: list[dict[str, str]], scene_id: int | None, now: int
    ) -> dict[str, int]:
        """按标题新增或改写；正文为空表示删除。"""
        counts = {"written": 0, "deleted": 0}
        for item in items:
            if not item["body"]:
                await self.db.execute(
                    "DELETE FROM story_notes WHERE bot_id=? AND title=?", (bot_id, item["title"])
                )
                counts["deleted"] += 1
                continue
            await self.db.execute(
                "INSERT INTO story_notes(bot_id, title, body, scene_id, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(bot_id, title) DO UPDATE SET "
                "body=excluded.body, scene_id=excluded.scene_id, updated_at=excluded.updated_at",
                (bot_id, item["title"], item["body"], scene_id, now, now),
            )
            counts["written"] += 1
        return counts

    # ------------------------------------------------------------------ 场景记录
    async def insert_scene(
        self,
        bot_id: str,
        *,
        cause: str,
        status: str,
        started_at: int,
        ended_at: int,
        rounds: int = 0,
        calls: int = 0,
        seed: Any = None,
        rolls: Any = None,
        transcript: Any = None,
        backfill: str = "",
        summary: str = "",
        salience: int | None = None,
        event_id: int | None = None,
        backfill_event_id: int | None = None,
        error: str = "",
    ) -> int:
        return await self.db.execute(
            "INSERT INTO story_scenes(bot_id, cause, status, started_at, ended_at, rounds, calls, "
            "seed, rolls, transcript, backfill, summary, salience, event_id, backfill_event_id, "
            "error, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                bot_id,
                cause,
                status,
                started_at,
                ended_at,
                int(rounds),
                int(calls),
                _dumps(seed) if seed is not None else None,
                _dumps(rolls) if rolls is not None else None,
                _dumps(transcript) if transcript is not None else None,
                backfill or None,
                summary or None,
                salience,
                event_id,
                backfill_event_id,
                error[:500] or None,
                ended_at,
            ),
        )

    async def recent_scenes(
        self, bot_id: str, limit: int, *, ok_only: bool = True
    ) -> list[dict[str, Any]]:
        """最近的场景，新的在前；附带对应记忆事件的碎片（她的视角）。"""
        if limit <= 0:
            return []
        where = "s.bot_id=?" + (" AND s.status='ok'" if ok_only else "")
        return await self.db.fetchall(
            "SELECT s.id, s.cause, s.status, s.started_at, s.ended_at, s.rounds, s.calls, "
            "s.summary, s.salience, s.event_id, s.error, e.fragment FROM story_scenes s "
            f"LEFT JOIN events e ON e.id=s.event_id WHERE {where} ORDER BY s.id DESC LIMIT ?",
            (bot_id, int(limit)),
        )

    async def last_transcript(self, bot_id: str) -> list[dict[str, Any]]:
        """上一场演成了的场景的完整转录（含她的内心，给谁看由调用方筛）。"""
        row = await self.db.fetchone(
            "SELECT transcript FROM story_scenes WHERE bot_id=? AND status='ok' "
            "ORDER BY id DESC LIMIT 1",
            (bot_id,),
        )
        transcript = _loads((row or {}).get("transcript"), [])
        return [turn for turn in transcript if isinstance(turn, dict)] if isinstance(transcript, list) else []

    async def scene_count(self, bot_id: str) -> int:
        value = await self.db.scalar(
            "SELECT COUNT(*) FROM story_scenes WHERE bot_id=? AND status='ok'",
            (bot_id,),
            default=0,
        )
        return int(value or 0)

    # ------------------------------------------------------------------ 聊天摘要
    async def chat_digest(self, bot_id: str, since: int, limit: int) -> list[str]:
        """她最近在聊天里经历的事（碎片或摘要），给背景模型当外显信息用。"""
        if limit <= 0:
            return []
        rows = await self.db.fetchall(
            "SELECT fragment, summary FROM events WHERE bot_id=? AND source<>'story' "
            "AND status='closed' AND kind='episodic' AND ended_at>=? "
            "ORDER BY ended_at DESC, id DESC LIMIT ?",
            (bot_id, int(since), int(limit)),
        )
        lines: list[str] = []
        for row in reversed(rows):
            text = str(row.get("summary") or row.get("fragment") or "").strip()
            if text:
                lines.append(" ".join(text.split())[:120])
        return lines

    # ------------------------------------------------------------------ 导出
    async def export(self, bot_id: str) -> dict[str, Any]:
        state = await self.state(bot_id)
        scenes = await self.db.fetchall(
            "SELECT * FROM story_scenes WHERE bot_id=? ORDER BY id DESC LIMIT 2000", (bot_id,)
        )
        for scene in scenes:
            for key in ("seed", "rolls", "transcript"):
                scene[key] = _loads(scene.get(key), None)
        exported_state = dict(state or {})
        if exported_state:
            exported_state["card"] = _loads(exported_state.get("card"), None)
            exported_state["seed_meta"] = _loads(exported_state.get("seed_meta"), {})
        return {
            "version": "shadow_story/export/v1",
            "bot_id": bot_id,
            "exported_at": self.now_fn(),
            "state": exported_state,
            "canon": await self.canon(bot_id),
            "notes": await self.notes(bot_id),
            "scenes": scenes,
        }
