"""在机器人之间复制记忆：人格/事实/事件/洞察/笔记（+ 媒体文件）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from ..interaction.media_transfer import MediaTransfer
from .edges import EdgeRepo, entities_from_json
from .persona import LAYERS, PersonaService
from .seed import _remap_source_ids

CATEGORIES = ("persona", "facts", "events", "insights", "notes", "media")
MAX_MEDIA_BYTES = 100 * 1024 * 1024
MAX_MEDIA_FILES = 500


class TransferError(ValueError):
    pass


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split())


class TransferService:
    def __init__(
        self,
        db: Database,
        configs: Callable[[str], ConfigView],
        persona: PersonaService,
        edges: EdgeRepo,
        media_transfer: MediaTransfer,
        audit: Audit,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.persona = persona
        self.edges = edges
        self.media_transfer = media_transfer
        self.audit = audit
        self.now_fn = now_fn

    # ------------------------------------------------------------------ 读取
    async def _source(self, bot_id: str) -> dict[str, list[dict[str, Any]]]:
        return {
            "persona": await self.db.fetchall(
                "SELECT layer, text, source_event_ids FROM persona_entries "
                "WHERE bot_id=? AND active=1 ORDER BY id",
                (bot_id,),
            ),
            "facts": await self.db.fetchall(
                "SELECT * FROM facts WHERE bot_id=? AND valid_to IS NULL AND archived=0 "
                "ORDER BY id",
                (bot_id,),
            ),
            "events": await self.db.fetchall(
                "SELECT * FROM events WHERE bot_id=? AND merged_into IS NULL ORDER BY id",
                (bot_id,),
            ),
            "insights": await self.db.fetchall(
                "SELECT * FROM insights WHERE bot_id=? ORDER BY id", (bot_id,)
            ),
            "notes": await self.db.fetchall(
                "SELECT * FROM notes WHERE bot_id=? ORDER BY id", (bot_id,)
            ),
        }

    async def _target_keys(self, bot_id: str) -> dict[str, Any]:
        persona = {
            (str(row["layer"]), _norm(row["text"]))
            for row in await self.db.fetchall(
                "SELECT layer, text FROM persona_entries WHERE bot_id=? AND active=1",
                (bot_id,),
            )
        }
        facts = {
            _norm(row["text"])
            for row in await self.db.fetchall(
                "SELECT text FROM facts WHERE bot_id=? AND valid_to IS NULL AND archived=0",
                (bot_id,),
            )
        }
        events: dict[tuple[str, Any], int] = {}
        for row in await self.db.fetchall(
            "SELECT id, fragment, ended_at FROM events WHERE bot_id=? AND merged_into IS NULL",
            (bot_id,),
        ):
            events[(_norm(row["fragment"]), row["ended_at"])] = int(row["id"])
        insights = {
            _norm(row["text"])
            for row in await self.db.fetchall("SELECT text FROM insights WHERE bot_id=?", (bot_id,))
        }
        notes = {
            (str(row["kind"]), str(row["title"] or ""), _norm(row["body"]))
            for row in await self.db.fetchall(
                "SELECT kind, title, body FROM notes WHERE bot_id=?", (bot_id,)
            )
        }
        return {
            "persona": persona,
            "facts": facts,
            "events": events,
            "insights": insights,
            "notes": notes,
        }

    # ------------------------------------------------------------------ 预览
    async def preview(
        self, source: str, target: str, categories: Iterable[str], mode: str
    ) -> dict[str, Any]:
        self._validate(source, target, categories, mode)
        cats = set(categories)
        data = await self._source(source)
        keys = await self._target_keys(target)
        counts: dict[str, int] = {}
        skip: dict[str, int] = {}
        for cat in ("persona", "facts", "events", "insights", "notes"):
            if cat not in cats:
                counts[cat] = 0
                skip[cat] = 0
                continue
            rows = data[cat]
            counts[cat] = len(rows)
            skip[cat] = 0 if mode == "replace" else self._skip_count(cat, rows, keys)
        media = {"items": 0, "files": 0, "bytes": 0, "missing": 0, "over_limit": False}
        if "media" in cats:
            media.update(await self.media_transfer.preview(source, target))
            media["over_limit"] = (
                media.get("files", 0) > MAX_MEDIA_FILES
                or media.get("bytes", 0) > MAX_MEDIA_BYTES
            )
        return {
            "source": source,
            "target": target,
            "mode": mode,
            "categories": sorted(cats),
            "counts": counts,
            "skip": skip,
            "media": media,
        }

    @staticmethod
    def _validate(source: str, target: str, categories: Iterable[str], mode: str) -> None:
        if not source or not target:
            raise TransferError("source / target 不能为空")
        if source == target:
            raise TransferError("不能转移给同一个机器人")
        cats = set(categories)
        if not cats or not cats <= set(CATEGORIES):
            raise TransferError("categories 不合法")
        if mode not in {"merge", "replace"}:
            raise TransferError("mode 只能是 merge / replace")

    @staticmethod
    def _skip_count(cat: str, rows: list[dict[str, Any]], keys: dict[str, Any]) -> int:
        skip = 0
        for row in rows:
            if cat == "persona":
                hit = (str(row["layer"]), _norm(row["text"])) in keys["persona"]
            elif cat == "facts":
                hit = _norm(row["text"]) in keys["facts"]
            elif cat == "events":
                hit = (_norm(row["fragment"]), row["ended_at"]) in keys["events"]
            elif cat == "insights":
                hit = _norm(row["text"]) in keys["insights"]
            else:
                hit = (
                    str(row["kind"]),
                    str(row["title"] or ""),
                    _norm(row["body"]),
                ) in keys["notes"]
            skip += 1 if hit else 0
        return skip

    # ------------------------------------------------------------------ 执行
    async def execute(
        self,
        source: str,
        target: str,
        categories: Iterable[str],
        mode: str,
        *,
        backup_path: str | Path | None = None,
        confirm_target: str | None = None,
    ) -> dict[str, Any]:
        self._validate(source, target, categories, mode)
        cats = set(categories)
        if mode == "replace" and confirm_target != target:
            raise TransferError("覆盖模式必须手动输入目标 bot_id")
        data = await self._source(source)
        if not any(data[cat] for cat in cats if cat != "media") and "media" not in cats:
            raise TransferError("源机器人没有可转移的内容")

        media_plan: dict[str, Any] | None = None
        if "media" in cats:
            preview = await self.media_transfer.preview(source, target)
            if preview.get("files", 0) > MAX_MEDIA_FILES or preview.get("bytes", 0) > MAX_MEDIA_BYTES:
                raise TransferError(
                    f"媒体超过转移上限（{MAX_MEDIA_FILES} 文件 / {MAX_MEDIA_BYTES // 1048576} MB）"
                )

        if backup_path is not None:
            await self.db.backup(backup_path)
        await self.persona.snapshot(target, "before_transfer")

        keys = await self._target_keys(target)
        counts = {"persona": 0, "facts": 0, "events": 0, "insights": 0, "notes": 0, "media": 0}
        stamps = self.now_fn()
        day = clock_mod.day_index(stamps)
        id_map: dict[int, int] = {}
        inserted_events: list[tuple[int, list[str]]] = []

        try:
            if "media" in cats:
                media_plan = await self.media_transfer.prepare(
                    source, target, replace=(mode == "replace")
                )
            async with self.db.transaction():
                if mode == "replace":
                    if "events" in cats:
                        await self.db.execute("DELETE FROM events WHERE bot_id=?", (target,))
                    if "facts" in cats:
                        await self.db.execute("DELETE FROM facts WHERE bot_id=?", (target,))
                    if "persona" in cats:
                        await self.db.execute(
                            "DELETE FROM persona_entries WHERE bot_id=?", (target,)
                        )
                    if "insights" in cats:
                        await self.db.execute("DELETE FROM insights WHERE bot_id=?", (target,))
                    if "notes" in cats:
                        await self.db.execute("DELETE FROM notes WHERE bot_id=?", (target,))
                    if "media" in cats:
                        await self.db.execute("DELETE FROM media_items WHERE bot_id=?", (target,))

                if "events" in cats:
                    for row in data["events"]:
                        key = (_norm(row["fragment"]), row["ended_at"])
                        if mode == "merge" and key in keys["events"]:
                            id_map[int(row["id"])] = keys["events"][key]
                            continue
                        new_id = await self._insert_event(target, row)
                        id_map[int(row["id"])] = new_id
                        if row.get("entities"):
                            inserted_events.append(
                                (new_id, entities_from_json(row.get("entities")))
                            )
                        counts["events"] += 1

                if "facts" in cats:
                    for row in data["facts"]:
                        if mode == "merge" and _norm(row["text"]) in keys["facts"]:
                            continue
                        await self._insert_fact(target, row, id_map, stamps)
                        counts["facts"] += 1

                if "persona" in cats:
                    for row in data["persona"]:
                        key = (str(row["layer"]), _norm(row["text"]))
                        if mode == "merge" and key in keys["persona"]:
                            continue
                        await self.db.execute(
                            "INSERT INTO persona_entries(bot_id, layer, text, source_event_ids, "
                            "created_day, last_edited_day, active) VALUES(?,?,?,?,?,?,1)",
                            (
                                target,
                                row["layer"],
                                row["text"],
                                _remap_source_ids(row.get("source_event_ids"), id_map),
                                day,
                                day,
                            ),
                        )
                        counts["persona"] += 1

                if "insights" in cats:
                    for row in data["insights"]:
                        if mode == "merge" and _norm(row["text"]) in keys["insights"]:
                            continue
                        await self.db.execute(
                            "INSERT INTO insights(bot_id, text, source_event_ids, "
                            "used_in_version, created_at) VALUES(?,?,?,NULL,?)",
                            (
                                target,
                                row["text"],
                                _remap_source_ids(row.get("source_event_ids"), id_map),
                                row.get("created_at") or stamps,
                            ),
                        )
                        counts["insights"] += 1

                if "notes" in cats:
                    for row in data["notes"]:
                        key = (str(row["kind"]), str(row["title"] or ""), _norm(row["body"]))
                        if mode == "merge" and key in keys["notes"]:
                            continue
                        await self.db.execute(
                            "INSERT INTO notes(bot_id, kind, title, body, tags, created_at, "
                            "updated_at) VALUES(?,?,?,?,?,?,?)",
                            (
                                target,
                                row["kind"],
                                row["title"],
                                row["body"],
                                row.get("tags"),
                                row.get("created_at") or stamps,
                                row.get("updated_at") or stamps,
                            ),
                        )
                        counts["notes"] += 1

                if media_plan is not None:
                    for row in media_plan["rows"]:
                        await self.db.execute(
                            "INSERT INTO media_items(bot_id, filename, description, tags, source, "
                            "source_ref, sender_id, sender_name, source_umo, size, created_at, "
                            "last_used_at, use_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                target,
                                row["filename"],
                                row.get("description") or "",
                                row.get("tags"),
                                row.get("source"),
                                row.get("source_ref"),
                                row.get("sender_id"),
                                row.get("sender_name"),
                                row.get("source_umo"),
                                int(row.get("size") or 0),
                                row.get("created_at") or stamps,
                                row.get("last_used_at"),
                                int(row.get("use_count") or 0),
                            ),
                        )
                        counts["media"] += 1
        except Exception:
            if media_plan is not None:
                await self.media_transfer.rollback(media_plan)
            raise

        for event_id, entities in inserted_events:
            try:
                await self.edges.link(
                    target,
                    event_id,
                    entities,
                    stamps,
                    max_degree=self.configs(target).int("edges.max_degree", 20),
                )
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("转移建边失败：%s", exc)

        result = {
            "source": source,
            "target": target,
            "mode": mode,
            "counts": counts,
            "backup": str(backup_path) if backup_path else "",
            "trash": str(media_plan.get("trash")) if media_plan and media_plan.get("trash") else "",
            "media": {
                "copied": len(media_plan["rows"]) if media_plan else 0,
                "skipped": media_plan.get("skipped", 0) if media_plan else 0,
                "renamed": media_plan.get("renamed", 0) if media_plan else 0,
                "failed": media_plan.get("failed", []) if media_plan else [],
                "bytes": media_plan.get("bytes", 0) if media_plan else 0,
            },
        }
        self.audit.event("memory_transfer", bot_id=target, source=source, mode=mode, **counts)
        return result

    async def _insert_event(self, bot_id: str, row: dict[str, Any]) -> int:
        return await self.db.execute(
            "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, participants, "
            "turn_count, char_count, fragment, summary, excerpts, entities, source_event_ids, "
            "storage_strength, retrievability, last_recalled_at, last_injected_at, salience, "
            "conflict, truncated, status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?)",
            (
                bot_id,
                "",
                row.get("kind") or "episodic",
                int(row.get("started_at") or row.get("ended_at") or self.now_fn()),
                row.get("ended_at"),
                row.get("participants"),
                int(row.get("turn_count") or 0),
                int(row.get("char_count") or len(str(row.get("fragment") or ""))),
                row.get("fragment"),
                row.get("summary"),
                row.get("excerpts"),
                row.get("entities"),
                float(row.get("storage_strength") or 7.0),
                float(row.get("retrievability") or 1.0),
                row.get("last_recalled_at"),
                row.get("last_injected_at"),
                row.get("salience"),
                int(row.get("conflict") or 0),
                int(row.get("truncated") or 0),
                row.get("status") or "closed",
                int(row.get("created_at") or self.now_fn()),
            ),
        )

    async def _insert_fact(
        self,
        bot_id: str,
        row: dict[str, Any],
        id_map: dict[int, int],
        stamp: int,
    ) -> None:
        source = row.get("source_event_id")
        try:
            source = int(source) if source is not None else None
        except (TypeError, ValueError):
            source = None
        await self.db.execute(
            "INSERT INTO facts(bot_id, kind, subject_id, text, key_fact, storage_strength, "
            "retrievability, last_recalled_at, valid_from, valid_to, superseded_by, archived, "
            "source_event_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                bot_id,
                row.get("kind") or "world",
                row.get("subject_id"),
                row.get("text"),
                int(row.get("key_fact") or 0),
                float(row.get("storage_strength") or 30.0),
                float(row.get("retrievability") or 1.0),
                row.get("last_recalled_at"),
                row.get("valid_from") or stamp,
                row.get("valid_to"),
                row.get("superseded_by"),
                int(row.get("archived") or 0),
                id_map.get(source) if source is not None else None,
                row.get("created_at") or stamp,
            ),
        )
