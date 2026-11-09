"""人格记忆：条目存储、叙事渲染、配额、快照与回滚。"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from ..core.types import OpResult, PersonaEntry

LAYERS = ("core", "edge", "tension")
LAYER_LABELS = {"core": "核心倾向", "edge": "边缘态度", "tension": "未消解张力"}
ConfigProvider = Callable[[str], ConfigView]


def _source_list(raw: Any) -> list[int]:
    if isinstance(raw, list):
        return [int(value) for value in raw if isinstance(value, (int, float))]
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return []
        if isinstance(value, list):
            return [int(item) for item in value if isinstance(item, (int, float))]
    return []


class PersonaService:
    def __init__(
        self,
        db: Database,
        configs: ConfigProvider,
        audit: Audit,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.audit = audit
        self.now_fn = now_fn

    # ------------------------------------------------------------------ 读取
    async def entries(self, bot_id: str) -> list[PersonaEntry]:
        rows = await self.db.fetchall(
            "SELECT * FROM persona_entries WHERE bot_id=? AND active=1 ORDER BY id", (bot_id,)
        )
        order = {layer: index for index, layer in enumerate(LAYERS)}
        result = [
            PersonaEntry(
                id=int(row["id"]),
                bot_id=str(row["bot_id"]),
                layer=str(row["layer"]),
                text=str(row["text"]),
                source_event_ids=_source_list(row.get("source_event_ids")),
                created_day=int(row.get("created_day") or 0),
                last_edited_day=int(row.get("last_edited_day") or 0),
            )
            for row in rows
            if str(row.get("layer")) in order
        ]
        result.sort(key=lambda entry: (order[entry.layer], entry.id))
        return result

    async def render(self, bot_id: str) -> str:
        entries = await self.entries(bot_id)
        return "\n".join(entry.text.strip() for entry in entries if entry.text.strip())

    async def counts(self, bot_id: str) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT layer, COUNT(*) AS n FROM persona_entries WHERE bot_id=? AND active=1 GROUP BY layer",
            (bot_id,),
        )
        counts = {layer: 0 for layer in LAYERS}
        for row in rows:
            layer = str(row.get("layer"))
            if layer in counts:
                counts[layer] = int(row.get("n") or 0)
        return counts

    async def char_count(self, bot_id: str) -> int:
        return len(await self.render(bot_id))

    async def version(self, bot_id: str) -> int:
        value = await self.db.scalar(
            "SELECT MAX(id) FROM persona_versions WHERE bot_id=?", (bot_id,), default=0
        )
        return int(value or 0)

    async def versions(self, bot_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            "SELECT id, created_at, reason, digest FROM persona_versions WHERE bot_id=? "
            "ORDER BY id DESC LIMIT ?",
            (bot_id, max(1, int(limit))),
        )

    # ------------------------------------------------------------------ 配额
    def _quotas(self, cfg: ConfigView) -> dict[str, int]:
        return {
            "core": cfg.int("persona.quota_core", 6, lo=1, hi=20),
            "edge": cfg.int("persona.quota_edge", 7, lo=1, hi=20),
            "tension": cfg.int("persona.quota_tension", 3, lo=1, hi=20),
        }

    # ------------------------------------------------------------------ 写入
    async def add_tension(
        self, bot_id: str, text: str, source_event_ids: Iterable[int], now: int | None = None
    ) -> int | None:
        cfg = self.configs(bot_id)
        body = (text or "").strip()
        if not body:
            return None
        counts = await self.counts(bot_id)
        quotas = self._quotas(cfg)
        if counts.get("tension", 0) >= quotas["tension"]:
            self.audit.warn("张力层已满（%s），本次不写入", quotas["tension"])
            return None
        stamp = now or self.now_fn()
        day = clock_mod.day_index(stamp)
        return await self.db.execute(
            "INSERT INTO persona_entries(bot_id, layer, text, source_event_ids, created_day, "
            "last_edited_day, active) VALUES(?,?,?,?,?,?,1)",
            (
                bot_id,
                "tension",
                body[:120],
                json.dumps(sorted({int(value) for value in source_event_ids}), ensure_ascii=False),
                day,
                day,
            ),
        )

    async def apply_ops(
        self, bot_id: str, ops: list[dict[str, Any]], *, now: int | None = None
    ) -> OpResult:
        cfg = self.configs(bot_id)
        quotas = self._quotas(cfg)
        entries = await self.entries(bot_id)
        by_id = {entry.id: entry for entry in entries}
        core_count = sum(1 for entry in entries if entry.layer == "core")
        edge_count = sum(1 for entry in entries if entry.layer == "edge")
        tension_count = sum(1 for entry in entries if entry.layer == "tension")
        total_chars = sum(len(entry.text) for entry in entries)

        min_chars = cfg.int("persona.entry_min_chars", 50, lo=10, hi=500)
        max_chars = cfg.int("persona.entry_max_chars", 70, lo=20, hi=1000)
        hard_chars = cfg.int("persona.hard_chars", 1600, lo=200, hi=10000)
        max_ops = cfg.int("persona.max_ops", 3, lo=1, hi=10)
        max_deletes = cfg.int("persona.max_deletes", 1, lo=0, hi=10)

        staged: list[dict[str, Any]] = []
        rejected: list[str] = []
        deletes = 0
        for index, raw in enumerate(ops):
            if index >= max_ops:
                rejected.append(f"operation {index}: 超出单次操作上限")
                continue
            if not isinstance(raw, dict):
                rejected.append(f"operation {index}: 不是对象")
                continue
            kind = str(raw.get("op") or "").lower()
            if kind == "delete":
                if deletes >= max_deletes:
                    rejected.append(f"operation {index}: 删除超出上限")
                    continue
                entry = by_id.get(int(raw.get("id") or 0))
                if entry is None:
                    rejected.append(f"operation {index}: 条目不存在")
                    continue
                deletes += 1
                total_chars -= len(entry.text)
                staged.append({"kind": "delete", "id": entry.id})
                continue
            if kind == "move":
                entry = by_id.get(int(raw.get("id") or 0))
                target = str(raw.get("to_layer") or "")
                if entry is None or target not in LAYERS:
                    rejected.append(f"operation {index}: move 参数不合法")
                    continue
                if target != entry.layer:
                    if target == "core":
                        core_count += 1
                        if entry.layer == "edge":
                            edge_count -= 1
                    elif target == "edge":
                        edge_count += 1
                        if entry.layer == "core":
                            core_count -= 1
                    elif target == "tension":
                        tension_count += 1
                        if entry.layer == "edge":
                            edge_count -= 1
                staged.append({"kind": "move", "id": entry.id, "to_layer": target})
                continue
            if kind in {"add", "rewrite"}:
                text = str(raw.get("text") or "").strip()
                target_id = int(raw.get("id") or 0)
                target = by_id.get(target_id) if target_id else None
                layer = str(raw.get("layer") or (target.layer if target else ""))
                if layer not in LAYERS:
                    rejected.append(f"operation {index}: layer 不合法")
                    continue
                if not (min_chars <= len(text) <= max_chars):
                    rejected.append(f"operation {index}: 文本长度 {len(text)} 不在 {min_chars}-{max_chars}")
                    continue
                sources = raw.get("source_event_ids")
                if not isinstance(sources, list) or not sources:
                    rejected.append(f"operation {index}: 缺少来源")
                    continue
                if kind == "add":
                    if layer == "core":
                        core_count += 1
                    elif layer == "edge":
                        edge_count += 1
                    else:
                        tension_count += 1
                    total_chars += len(text)
                    staged.append({"kind": "add", "layer": layer, "text": text, "sources": sources})
                else:
                    entry = by_id.get(int(raw.get("id") or 0))
                    if entry is None:
                        rejected.append(f"operation {index}: 条目不存在")
                        continue
                    total_chars += len(text) - len(entry.text)
                    staged.append({"kind": "rewrite", "id": entry.id, "text": text, "sources": sources})
                continue
            rejected.append(f"operation {index}: 未知操作 {kind}")

        quota_errors: list[str] = []
        if core_count > quotas["core"]:
            quota_errors.append("core 超出配额")
        if edge_count > quotas["edge"]:
            quota_errors.append("edge 超出配额")
        if tension_count > quotas["tension"]:
            quota_errors.append("tension 超出配额")
        if total_chars > hard_chars:
            quota_errors.append(f"总量 {total_chars} 超过硬线 {hard_chars}")
        if quota_errors:
            return OpResult(applied=0, rejected=quota_errors + rejected)
        if not staged:
            return OpResult(applied=0, rejected=rejected)

        stamp = now or self.now_fn()
        day = clock_mod.day_index(stamp)
        if total_chars > cfg.int("persona.warn_chars", 1300, lo=200, hi=10000):
            self.audit.warn("人格块接近上限：%s 字", total_chars)

        applied = 0
        for operation in staged:
            if operation["kind"] == "delete":
                await self.db.execute(
                    "UPDATE persona_entries SET active=0, last_edited_day=? WHERE id=?",
                    (day, operation["id"]),
                )
            elif operation["kind"] == "move":
                await self.db.execute(
                    "UPDATE persona_entries SET layer=?, last_edited_day=? WHERE id=?",
                    (operation["to_layer"], day, operation["id"]),
                )
            elif operation["kind"] == "rewrite":
                await self.db.execute(
                    "UPDATE persona_entries SET text=?, source_event_ids=?, last_edited_day=? WHERE id=?",
                    (
                        operation["text"],
                        json.dumps(
                            sorted({int(value) for value in operation["sources"]}),
                            ensure_ascii=False,
                        ),
                        day,
                        operation["id"],
                    ),
                )
            elif operation["kind"] == "add":
                await self.db.execute(
                    "INSERT INTO persona_entries(bot_id, layer, text, source_event_ids, "
                    "created_day, last_edited_day, active) VALUES(?,?,?,?,?,?,1)",
                    (
                        bot_id,
                        operation["layer"],
                        operation["text"],
                        json.dumps(
                            sorted({int(value) for value in operation["sources"]}),
                            ensure_ascii=False,
                        ),
                        day,
                        day,
                    ),
                )
            applied += 1
        return OpResult(applied=applied, rejected=rejected)

    # ------------------------------------------------------------------ 快照
    async def snapshot(self, bot_id: str, reason: str, applied_ops: Any = None) -> int:
        entries = await self.entries(bot_id)
        payload = [
            {
                "id": entry.id,
                "layer": entry.layer,
                "text": entry.text,
                "source_event_ids": entry.source_event_ids,
                "created_day": entry.created_day,
                "last_edited_day": entry.last_edited_day,
            }
            for entry in entries
        ]
        digest = f"{len(entries)}:{sum(len(entry.text) for entry in entries)}"
        stamp = self.now_fn()
        version_id = await self.db.execute(
            "INSERT INTO persona_versions(bot_id, created_at, reason, snapshot, applied_ops, digest) "
            "VALUES(?,?,?,?,?,?)",
            (
                bot_id,
                stamp,
                reason,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(applied_ops, ensure_ascii=False) if applied_ops is not None else None,
                digest,
            ),
        )
        await self.prune_versions(bot_id)
        return version_id

    async def restore(self, bot_id: str, version_id: int) -> bool:
        row = await self.db.fetchone(
            "SELECT * FROM persona_versions WHERE id=? AND bot_id=?", (int(version_id), bot_id)
        )
        if row is None:
            return False
        try:
            payload = json.loads(row.get("snapshot") or "[]")
        except (ValueError, TypeError):
            return False
        if not isinstance(payload, list):
            return False
        day = clock_mod.day_index(self.now_fn())
        async with self.db.transaction():
            await self.db.execute("DELETE FROM persona_entries WHERE bot_id=?", (bot_id,))
            for item in payload:
                if not isinstance(item, dict):
                    continue
                await self.db.execute(
                    "INSERT INTO persona_entries(id, bot_id, layer, text, source_event_ids, "
                    "created_day, last_edited_day, active) VALUES(?,?,?,?,?,?,?,1)",
                    (
                        int(item.get("id") or 0),
                        bot_id,
                        str(item.get("layer") or "edge"),
                        str(item.get("text") or ""),
                        json.dumps(item.get("source_event_ids") or [], ensure_ascii=False),
                        int(item.get("created_day") or day),
                        int(item.get("last_edited_day") or day),
                    ),
                )
        await self.snapshot(bot_id, f"before_rollback:{version_id}")
        return True

    async def prune_versions(self, bot_id: str) -> None:
        cfg = self.configs(bot_id)
        keep = cfg.int("persona.snapshot_keep", 20, lo=1, hi=500)
        rows = await self.db.fetchall(
            "SELECT id, created_at FROM persona_versions WHERE bot_id=? ORDER BY id DESC", (bot_id,)
        )
        if len(rows) <= keep:
            return
        keep_ids: set[int] = {int(row["id"]) for row in rows[:keep]}
        seen_months: set[str] = set()
        for row in rows[keep:]:
            month = clock_mod.iso(int(row["created_at"]))[:7]
            if month in seen_months:
                continue
            seen_months.add(month)
            keep_ids.add(int(row["id"]))
        stale = [int(row["id"]) for row in rows if int(row["id"]) not in keep_ids]
        if not stale:
            return
        placeholders = ",".join("?" for _ in stale)
        await self.db.execute(
            f"DELETE FROM persona_versions WHERE id IN ({placeholders})", tuple(stale)
        )

    async def reset(self, bot_id: str) -> None:
        await self.db.execute("DELETE FROM persona_entries WHERE bot_id=?", (bot_id,))
