"""初始人格记忆（persona_seed v1）的校验、导入与导出。

seed 是给外部 LLM 用的可移植格式：人格三层 + 事实 + 可选情景。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from .dynamics import Dynamics
from .edges import EdgeRepo, normalize_entities
from .facts import FACT_KINDS, KEY_FACT_TYPES, FactService
from .persona import LAYERS, PersonaService

SEED_VERSION = "persona_seed/v1"
FULL_VERSION = "shadow_memory/full/v1"
MAX_SEED_EVENTS = 20
MAX_FULL_ROWS = 2000


class SeedError(ValueError):
    """seed 文件不合法或当前库不允许导入。"""


def validate_seed(data: Any, cfg: ConfigView) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(data, dict):
        raise SeedError("seed 不是 JSON 对象")
    version = str(data.get("version") or "")
    if version and not version.startswith("persona_seed"):
        errors.append(f"version 不认识：{version}")

    min_chars = max(10, cfg.int("persona.entry_min_chars", 50) - 10)
    max_chars = cfg.int("persona.entry_max_chars", 70) + 20
    quotas = {
        "core": cfg.int("persona.quota_core", 6),
        "edge": cfg.int("persona.quota_edge", 7),
        "tension": cfg.int("persona.quota_tension", 3),
    }

    persona_raw = data.get("persona") or {}
    if not isinstance(persona_raw, dict):
        errors.append("persona 必须是对象")
        persona_raw = {}
    persona: dict[str, list[str]] = {layer: [] for layer in LAYERS}
    for layer in LAYERS:
        items = persona_raw.get(layer) or []
        if not isinstance(items, list):
            errors.append(f"persona.{layer} 必须是数组")
            continue
        limit = int(math.ceil(quotas[layer] * 1.5))
        for index, item in enumerate(items[: limit + 1]):
            text = str(item or "").strip()
            if not text:
                errors.append(f"persona.{layer}[{index}] 为空")
                continue
            if not (min_chars <= len(text) <= max_chars):
                errors.append(
                    f"persona.{layer}[{index}] 长度 {len(text)} 不在 {min_chars}-{max_chars}"
                )
                continue
            persona[layer].append(text)
        if len(persona[layer]) > limit:
            errors.append(f"persona.{layer} 超过上限 {limit}")

    facts: list[dict[str, Any]] = []
    facts_raw = data.get("facts") or []
    if not isinstance(facts_raw, list):
        errors.append("facts 必须是数组")
        facts_raw = []
    for index, item in enumerate(facts_raw[:50]):
        if not isinstance(item, dict):
            errors.append(f"facts[{index}] 不是对象")
            continue
        text = str(item.get("text") or "").strip()
        if not text or len(text) > 120:
            errors.append(f"facts[{index}] 文本为空或超过 120 字")
            continue
        kind = str(item.get("kind") or "world")
        if kind not in FACT_KINDS:
            kind = "world"
        key_type = str(item.get("key_fact_type") or "none").lower()
        salience = int(item.get("salience") or 3)
        facts.append(
            {
                "kind": kind,
                "subject_id": str(item.get("subject_id") or "") or None,
                "text": text,
                "key_fact": key_type in KEY_FACT_TYPES,
                "salience": min(5, max(1, salience)),
            }
        )

    events: list[dict[str, Any]] = []
    events_raw = data.get("events") or []
    if not isinstance(events_raw, list):
        errors.append("events 必须是数组")
        events_raw = []
    for index, item in enumerate(events_raw[:MAX_SEED_EVENTS]):
        if not isinstance(item, dict):
            errors.append(f"events[{index}] 不是对象")
            continue
        fragment = str(item.get("fragment") or "").strip()
        if not fragment or len(fragment) > 60:
            errors.append(f"events[{index}] 碎片为空或超过 60 字")
            continue
        summary = str(item.get("summary") or "").strip()[:300]
        entities = normalize_entities(item.get("entities"))
        days = item.get("occurred_days_ago")
        try:
            occurred_days = max(0.0, min(3650.0, float(days or 0)))
        except (TypeError, ValueError):
            occurred_days = 0.0
        salience = int(item.get("salience") or 3)
        participants = item.get("participants")
        events.append(
            {
                "fragment": fragment,
                "summary": summary,
                "entities": entities,
                "occurred_days": occurred_days,
                "salience": min(5, max(1, salience)),
                "participants": participants if isinstance(participants, list) else [],
            }
        )

    if errors:
        raise SeedError("；".join(errors[:12]))
    return {"version": SEED_VERSION, "persona": persona, "facts": facts, "events": events}


def _number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _full_entities(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return normalize_entities(raw)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return normalize_entities(value if isinstance(value, list) else [])


def _remap_source_ids(raw: Any, id_map: dict[int, int]) -> str:
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


def validate_full(data: Any) -> dict[str, Any]:
    """校验 shadow_memory/full/v1 备份，返回可直接入库的规范化结构。"""
    if not isinstance(data, dict):
        raise SeedError("备份不是 JSON 对象")
    version = str(data.get("version") or "")
    if not version.startswith("shadow_memory/full/"):
        raise SeedError(f"不是 full 备份：{version or '(空)'}")

    persona_raw = data.get("persona")
    if not isinstance(persona_raw, list):
        raise SeedError("persona 必须是数组")
    facts_raw = data.get("facts") or []
    events_raw = data.get("events") or []
    insights_raw = data.get("insights") or []
    notes_raw = data.get("notes") or []
    if not isinstance(facts_raw, list) or not isinstance(events_raw, list):
        raise SeedError("facts / events 必须是数组")
    if not isinstance(insights_raw, list) or not isinstance(notes_raw, list):
        raise SeedError("insights / notes 必须是数组")
    if len(facts_raw) > MAX_FULL_ROWS or len(events_raw) > MAX_FULL_ROWS:
        raise SeedError(f"备份过大：facts/events 各限 {MAX_FULL_ROWS} 条")
    if len(insights_raw) > MAX_FULL_ROWS or len(notes_raw) > MAX_FULL_ROWS:
        raise SeedError(f"备份过大：insights/notes 各限 {MAX_FULL_ROWS} 条")

    persona: list[dict[str, Any]] = []
    for index, row in enumerate(persona_raw):
        if not isinstance(row, dict):
            raise SeedError(f"persona[{index}] 不是对象")
        layer = str(row.get("layer") or "")
        text = str(row.get("text") or "").strip()
        if layer not in LAYERS:
            raise SeedError(f"persona[{index}] layer 不认识：{layer}")
        if not text:
            raise SeedError(f"persona[{index}] 为空")
        source = row.get("source_event_ids")
        persona.append(
            {
                "layer": layer,
                "text": text,
                "source_event_ids": source if isinstance(source, str) else "[]",
            }
        )

    facts: list[dict[str, Any]] = []
    for index, row in enumerate(facts_raw):
        if not isinstance(row, dict):
            raise SeedError(f"facts[{index}] 不是对象")
        text = str(row.get("text") or "").strip()
        if not text:
            raise SeedError(f"facts[{index}] 为空")
        kind = str(row.get("kind") or "world")
        if kind not in FACT_KINDS:
            kind = "world"
        facts.append(
            {
                "kind": kind,
                "subject_id": str(row.get("subject_id") or "") or None,
                "text": text,
                "key_fact": 1 if int(row.get("key_fact") or 0) else 0,
                "storage_strength": _number(row.get("storage_strength"), 30.0),
                "retrievability": _number(row.get("retrievability"), 1.0),
                "last_recalled_at": row.get("last_recalled_at"),
                "valid_from": row.get("valid_from"),
                "valid_to": row.get("valid_to"),
                "superseded_by": row.get("superseded_by"),
                "archived": int(row.get("archived") or 0),
                "source_event_id": row.get("source_event_id"),
                "created_at": row.get("created_at"),
            }
        )

    events: list[dict[str, Any]] = []
    for index, row in enumerate(events_raw):
        if not isinstance(row, dict):
            raise SeedError(f"events[{index}] 不是对象")
        old_id = row.get("id")
        try:
            old_id = int(old_id) if old_id is not None else None
        except (TypeError, ValueError):
            old_id = None
        ended = row.get("ended_at")
        try:
            ended = int(ended) if ended is not None else None
        except (TypeError, ValueError):
            ended = None
        salience = int(row.get("salience") or 3)
        events.append(
            {
                "old_id": old_id,
                "kind": str(row.get("kind") or "episodic"),
                "fragment": str(row.get("fragment") or ""),
                "summary": str(row.get("summary") or "") or None,
                "entities": _full_entities(row.get("entities")),
                "salience": min(5, max(1, salience)),
                "storage_strength": _number(row.get("storage_strength"), 7.0),
                "retrievability": _number(row.get("retrievability"), 1.0),
                "status": str(row.get("status") or "closed"),
                "ended_at": ended,
            }
        )

    insights: list[dict[str, Any]] = []
    for index, row in enumerate(insights_raw):
        if not isinstance(row, dict):
            raise SeedError(f"insights[{index}] 不是对象")
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        source = row.get("source_event_ids")
        insights.append(
            {
                "text": text,
                "source_event_ids": source
                if isinstance(source, str)
                else json.dumps(source or [], ensure_ascii=False),
            }
        )

    notes: list[dict[str, Any]] = []
    for index, row in enumerate(notes_raw):
        if not isinstance(row, dict):
            raise SeedError(f"notes[{index}] 不是对象")
        body = str(row.get("body") or "").strip()
        if not body:
            continue
        kind = str(row.get("kind") or "note")
        if kind not in {"note", "diary"}:
            kind = "note"
        created = row.get("created_at")
        updated = row.get("updated_at")
        notes.append(
            {
                "kind": kind,
                "title": str(row.get("title") or ""),
                "body": body,
                "tags": str(row.get("tags") or "") or None,
                "created_at": int(created) if isinstance(created, (int, float)) else None,
                "updated_at": int(updated) if isinstance(updated, (int, float)) else None,
            }
        )

    return {
        "version": version,
        "is_v2": version.startswith("shadow_memory/full/v2"),
        "persona": persona,
        "facts": facts,
        "events": events,
        "insights": insights,
        "notes": notes,
    }


class SeedService:
    def __init__(
        self,
        db: Database,
        configs: Callable[[str], ConfigView],
        persona: PersonaService,
        facts: FactService,
        edges: EdgeRepo,
        dynamics: Dynamics,
        audit: Audit,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.persona = persona
        self.facts = facts
        self.edges = edges
        self.dynamics = dynamics
        self.audit = audit
        self.now_fn = now_fn

    async def _existing(self, bot_id: str) -> int:
        persona_count = int(
            await self.db.scalar(
                "SELECT COUNT(*) FROM persona_entries WHERE bot_id=? AND active=1",
                (bot_id,),
                default=0,
            )
            or 0
        )
        event_count = int(
            await self.db.scalar(
                "SELECT COUNT(*) FROM events WHERE bot_id=?", (bot_id,), default=0
            )
            or 0
        )
        fact_count = int(
            await self.db.scalar("SELECT COUNT(*) FROM facts WHERE bot_id=?", (bot_id,), default=0)
            or 0
        )
        return persona_count + event_count + fact_count

    async def import_seed(
        self,
        bot_id: str,
        data: Any,
        *,
        force: bool = False,
        now: int | None = None,
    ) -> dict[str, int]:
        cfg = self.configs(bot_id)
        normalized = validate_seed(data, cfg)
        if await self._existing(bot_id) and not force:
            raise SeedError("当前机器人已有记忆，拒绝导入；确认覆盖请加 force")

        stamp = now or self.now_fn()
        if force:
            await self.persona.snapshot(bot_id, "before_seed_import")
        counts = {"persona": 0, "facts": 0, "events": 0}
        day = clock_mod.day_index(stamp)

        async with self.db.transaction():
            if force:
                await self.db.execute("DELETE FROM events WHERE bot_id=?", (bot_id,))
                await self.db.execute("DELETE FROM facts WHERE bot_id=?", (bot_id,))
                await self.db.execute("DELETE FROM insights WHERE bot_id=?", (bot_id,))
                await self.db.execute("DELETE FROM fact_candidates WHERE bot_id=?", (bot_id,))
                await self.db.execute("DELETE FROM persona_entries WHERE bot_id=?", (bot_id,))
            for layer in LAYERS:
                for text in normalized["persona"][layer]:
                    await self.db.execute(
                        "INSERT INTO persona_entries(bot_id, layer, text, source_event_ids, "
                        "created_day, last_edited_day, active) VALUES(?,?,?,?,?,?,1)",
                        (bot_id, layer, text, "[]", day, day),
                    )
                    counts["persona"] += 1
            for fact in normalized["facts"]:
                await self.facts.add_manual(
                    bot_id,
                    fact["text"],
                    kind=fact["kind"],
                    subject_id=fact["subject_id"],
                    key_fact=fact["key_fact"],
                    salience=fact["salience"],
                    now=stamp,
                )
                counts["facts"] += 1
            for event in normalized["events"]:
                occurred = stamp - int(event["occurred_days"] * 86_400_000)
                strength = self.dynamics.s0_event(event["salience"])
                await self.db.execute(
                    "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, participants, "
                    "turn_count, char_count, fragment, summary, entities, source_event_ids, "
                    "storage_strength, retrievability, last_recalled_at, salience, status, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?, 'closed', ?)",
                    (
                        bot_id,
                        "",
                        "episodic",
                        occurred,
                        occurred,
                        json.dumps(event["participants"], ensure_ascii=False),
                        0,
                        len(event["fragment"]),
                        event["fragment"],
                        event["summary"] or None,
                        json.dumps(event["entities"], ensure_ascii=False),
                        strength,
                        1.0,
                        stamp,
                        event["salience"],
                        occurred,
                    ),
                )
                counts["events"] += 1

        # 建边在事务外：失败不阻断导入
        for event in normalized["events"]:
            if not event["entities"]:
                continue
            row = await self.db.fetchone(
                "SELECT id FROM events WHERE bot_id=? AND fragment=? ORDER BY id DESC LIMIT 1",
                (bot_id, event["fragment"]),
            )
            if row:
                try:
                    await self.edges.link(
                        bot_id,
                        int(row["id"]),
                        event["entities"],
                        stamp,
                        max_degree=cfg.int("edges.max_degree", 20),
                    )
                except Exception as exc:  # noqa: BLE001
                    self.audit.warn("seed 事件建边失败：%s", exc)
        self.audit.event("seed_import", bot_id=bot_id, force=force, **counts)
        return counts

    async def export_seed(self, bot_id: str, now: int | None = None) -> dict[str, Any]:
        stamp = now or self.now_fn()
        entries = await self.persona.entries(bot_id)
        persona = {layer: [] for layer in LAYERS}
        for entry in entries:
            persona[entry.layer].append(entry.text)
        fact_rows = await self.db.fetchall(
            "SELECT kind, subject_id, text, key_fact, storage_strength, last_recalled_at "
            "FROM facts WHERE bot_id=? AND valid_to IS NULL AND archived=0 "
            "ORDER BY key_fact DESC, COALESCE(valid_from, created_at) DESC LIMIT 50",
            (bot_id,),
        )
        facts = []
        for row in fact_rows:
            key_type = "promise"
            if int(row.get("key_fact") or 0) == 0:
                key_type = "none"
            facts.append(
                {
                    "kind": row.get("kind") or "world",
                    "subject_id": row.get("subject_id") or "",
                    "text": row.get("text") or "",
                    "key_fact_type": key_type,
                    "salience": 3,
                }
            )
        event_rows = await self.db.fetchall(
            "SELECT fragment, summary, entities, ended_at, salience FROM events WHERE bot_id=? "
            "AND kind='episodic' AND merged_into IS NULL AND status='closed' "
            "ORDER BY ended_at DESC LIMIT 20",
            (bot_id,),
        )
        events = []
        for row in event_rows:
            try:
                entities = json.loads(row.get("entities") or "[]")
            except (ValueError, TypeError):
                entities = []
            events.append(
                {
                    "fragment": row.get("fragment") or "",
                    "summary": row.get("summary") or "",
                    "entities": entities if isinstance(entities, list) else [],
                    "occurred_days_ago": round(
                        max(0.0, (stamp - float(row.get("ended_at") or stamp)) / 86_400_000.0), 1
                    ),
                    "salience": int(row.get("salience") or 2),
                    "participants": [],
                }
            )
        return {
            "version": SEED_VERSION,
            "bot_hint": bot_id,
            "persona": persona,
            "facts": facts,
            "events": events,
        }

    async def restore_full(
        self,
        bot_id: str,
        data: Any,
        *,
        force: bool = False,
        backup_path: str | Path | None = None,
        now: int | None = None,
    ) -> dict[str, int]:
        """从 shadow_memory/full/v1 恢复人格、事实与事件；事件 id 会重映射。"""
        normalized = validate_full(data)
        cfg = self.configs(bot_id)
        if await self._existing(bot_id) and not force:
            raise SeedError("当前机器人已有记忆，拒绝恢复；确认覆盖请加 force")

        stamp = now or self.now_fn()
        if force:
            if backup_path is not None:
                await self.db.backup(backup_path)
            await self.persona.snapshot(bot_id, "before_full_restore")

        counts = {"persona": 0, "facts": 0, "events": 0, "insights": 0, "notes": 0}
        day = clock_mod.day_index(stamp)
        id_map: dict[int, int] = {}
        restored_events: list[tuple[int, list[str]]] = []

        async with self.db.transaction():
            if force:
                await self.db.execute("DELETE FROM events WHERE bot_id=?", (bot_id,))
                await self.db.execute("DELETE FROM facts WHERE bot_id=?", (bot_id,))
                await self.db.execute("DELETE FROM insights WHERE bot_id=?", (bot_id,))
                await self.db.execute("DELETE FROM fact_candidates WHERE bot_id=?", (bot_id,))
                await self.db.execute("DELETE FROM persona_entries WHERE bot_id=?", (bot_id,))
                if normalized["is_v2"]:
                    await self.db.execute("DELETE FROM notes WHERE bot_id=?", (bot_id,))
            for event in normalized["events"]:
                ended = event["ended_at"] or stamp
                new_id = await self.db.execute(
                    "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, participants, "
                    "turn_count, char_count, fragment, summary, entities, source_event_ids, "
                    "storage_strength, retrievability, last_recalled_at, salience, status, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?)",
                    (
                        bot_id,
                        "",
                        event["kind"],
                        ended,
                        ended,
                        "[]",
                        0,
                        len(event["fragment"]),
                        event["fragment"],
                        event["summary"],
                        json.dumps(event["entities"], ensure_ascii=False),
                        event["storage_strength"],
                        event["retrievability"],
                        ended,
                        event["salience"],
                        event["status"],
                        ended,
                    ),
                )
                if event["old_id"] is not None:
                    id_map[event["old_id"]] = new_id
                if event["entities"]:
                    restored_events.append((new_id, event["entities"]))
                counts["events"] += 1
            for fact in normalized["facts"]:
                source = fact["source_event_id"]
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
                        fact["kind"],
                        fact["subject_id"],
                        fact["text"],
                        fact["key_fact"],
                        fact["storage_strength"],
                        fact["retrievability"],
                        fact["last_recalled_at"],
                        fact["valid_from"] or stamp,
                        fact["valid_to"],
                        fact["superseded_by"],
                        fact["archived"],
                        id_map.get(source) if source is not None else None,
                        fact["created_at"] or stamp,
                    ),
                )
                counts["facts"] += 1
            for entry in normalized["persona"]:
                await self.db.execute(
                    "INSERT INTO persona_entries(bot_id, layer, text, source_event_ids, "
                    "created_day, last_edited_day, active) VALUES(?,?,?,?,?,?,1)",
                    (
                        bot_id,
                        entry["layer"],
                        entry["text"],
                        _remap_source_ids(entry["source_event_ids"], id_map),
                        day,
                        day,
                    ),
                )
                counts["persona"] += 1
            for insight in normalized["insights"]:
                await self.db.execute(
                    "INSERT INTO insights(bot_id, text, source_event_ids, used_in_version, created_at) "
                    "VALUES(?,?,?,NULL,?)",
                    (
                        bot_id,
                        insight["text"],
                        _remap_source_ids(insight["source_event_ids"], id_map),
                        stamp,
                    ),
                )
                counts["insights"] += 1
            for note in normalized["notes"]:
                created = note["created_at"] or stamp
                await self.db.execute(
                    "INSERT INTO notes(bot_id, kind, title, body, tags, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        bot_id,
                        note["kind"],
                        note["title"] or None,
                        note["body"],
                        note["tags"],
                        created,
                        note["updated_at"] or created,
                    ),
                )
                counts["notes"] += 1

        for event_id, entities in restored_events:
            try:
                await self.edges.link(
                    bot_id,
                    event_id,
                    entities,
                    stamp,
                    max_degree=cfg.int("edges.max_degree", 20),
                )
            except Exception as exc:  # noqa: BLE001 - 建边失败不阻断恢复
                self.audit.warn("full 恢复建边失败：%s", exc)
        self.audit.event("full_restore", bot_id=bot_id, force=force, **counts)
        return counts
