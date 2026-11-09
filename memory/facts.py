"""稳定认知：事实的写入、抽取、有效期、检索与衰减。"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from . import prompts
from .dynamics import Dynamics
from .textutil import bigrams as _bigrams
from .textutil import overlap as _overlap

KEY_FACT_TYPES = {"time", "address", "amount", "promise"}
FACT_KINDS = {"profile", "preference", "commitment", "world"}
ConfigProvider = Callable[[str], ConfigView]


class FactService:
    def __init__(
        self,
        db: Database,
        configs: ConfigProvider,
        dynamics: Dynamics,
        textgen: Any,
        audit: Audit,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.dynamics = dynamics
        self.textgen = textgen
        self.audit = audit
        self.now_fn = now_fn

    # ------------------------------------------------------------------ 候选队列
    async def enqueue(
        self, bot_id: str, umo: str, text: str, salience: int, now: int
    ) -> int:
        return await self.db.execute(
            "INSERT INTO fact_candidates(bot_id, umo, text, salience, created_at, consumed) "
            "VALUES(?,?,?,?,?,0)",
            (bot_id, umo, (text or "")[:300], min(5, max(1, int(salience))), now),
        )

    async def pending(self, bot_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            "SELECT * FROM fact_candidates WHERE bot_id=? AND consumed=0 ORDER BY id ASC LIMIT ?",
            (bot_id, max(1, int(limit))),
        )

    async def nightly_extract(self, bot_id: str, now: int | None = None) -> int:
        candidates = await self.pending(bot_id, 20)
        if not candidates:
            return 0
        stamp = now or self.now_fn()
        chunks = "\n---\n".join(str(row["text"]) for row in candidates)
        result = await self.textgen.generate_json(
            prompts.FACTS_SYSTEM,
            prompts.FACTS_USER.format(chunks=chunks[:6000]),
            bot_id=bot_id,
            schema_hint=prompts.FACTS_SCHEMA,
            task="facts",
        )
        if not isinstance(result, dict):
            self.audit.warn("事实抽取失败，候选保留到明晚（bot=%s）", bot_id)
            return 0
        raw_facts = result.get("facts")
        added = 0
        if isinstance(raw_facts, list):
            for item in raw_facts[:20]:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()
                if not text or len(text) > 120:
                    continue
                kind = str(item.get("kind") or "world")
                if kind not in FACT_KINDS:
                    kind = "world"
                key_type = str(item.get("key_fact_type") or "none").lower()
                key_fact = 1 if key_type in KEY_FACT_TYPES else 0
                salience = int(item.get("salience") or 2)
                salience = min(5, max(1, salience))
                if await self._duplicate(bot_id, text):
                    continue
                await self._insert(
                    bot_id=bot_id,
                    kind=kind,
                    subject_id=str(item.get("subject_id") or "") or None,
                    text=text,
                    key_fact=bool(key_fact),
                    salience=salience,
                    now=stamp,
                )
                added += 1
        await self.db.execute(
            "UPDATE fact_candidates SET consumed=1 WHERE bot_id=? AND id<=?",
            (bot_id, int(candidates[-1]["id"])),
        )
        self.audit.event("facts_extracted", bot_id=bot_id, added=added, candidates=len(candidates))
        return added

    async def _duplicate(self, bot_id: str, text: str) -> bool:
        row = await self.db.fetchone(
            "SELECT id FROM facts WHERE bot_id=? AND text=? AND valid_to IS NULL LIMIT 1",
            (bot_id, text),
        )
        return row is not None

    async def _insert(
        self,
        *,
        bot_id: str,
        kind: str,
        subject_id: str | None,
        text: str,
        key_fact: bool,
        salience: int,
        now: int,
        source_event_id: int | None = None,
    ) -> int:
        strength = 9999.0 if key_fact else self.dynamics.s0_fact(salience)
        return await self.db.execute(
            "INSERT INTO facts(bot_id, kind, subject_id, text, key_fact, storage_strength, "
            "retrievability, last_recalled_at, valid_from, valid_to, superseded_by, archived, "
            "source_event_id, created_at) VALUES(?,?,?,?,?,?,1.0,?,?,NULL,NULL,0,?,?)",
            (
                bot_id,
                kind,
                subject_id,
                text,
                1 if key_fact else 0,
                strength,
                now,
                now,
                source_event_id,
                now,
            ),
        )

    async def add_manual(
        self,
        bot_id: str,
        text: str,
        *,
        kind: str = "world",
        subject_id: str | None = None,
        key_fact: bool = False,
        salience: int = 3,
        now: int | None = None,
        source_event_id: int | None = None,
    ) -> int:
        return await self._insert(
            bot_id=bot_id,
            kind=kind if kind in FACT_KINDS else "world",
            subject_id=subject_id,
            text=text[:120],
            key_fact=key_fact,
            salience=min(5, max(1, int(salience))),
            now=now or self.now_fn(),
            source_event_id=source_event_id,
        )

    # ------------------------------------------------------------------ 读取
    async def _active_rows(self, bot_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            "SELECT * FROM facts WHERE bot_id=? AND valid_to IS NULL AND archived=0 "
            "ORDER BY key_fact DESC, id DESC LIMIT ?",
            (bot_id, max(1, int(limit))),
        )

    def _retrievability(self, row: dict[str, Any], now: int) -> float:
        if int(row.get("key_fact") or 0):
            return 1.0
        last = row.get("last_recalled_at") or row.get("valid_from") or row.get("created_at")
        return self.dynamics.r(float(row.get("storage_strength") or 0.0), last, now)

    async def relevant(
        self, bot_id: str, query: str, now: int, limit: int = 3
    ) -> list[dict[str, Any]]:
        cfg = self.configs(bot_id)
        floor = cfg.float("dynamics.r_floor", 0.2)
        tokens = _bigrams(query)
        rows = await self._active_rows(bot_id, 200)
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            text = str(row.get("text") or "")
            score = _overlap(tokens, text)
            if score <= 0:
                continue
            r_value = self._retrievability(row, now)
            if r_value < floor:
                continue
            scored.append((score * (0.5 + r_value), row))
        scored.sort(key=lambda item: item[0], reverse=True)
        result = []
        for _, row in scored[: max(1, int(limit))]:
            item = dict(row)
            item["r"] = self._retrievability(row, now)
            result.append(item)
        return result

    async def search(self, bot_id: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        tokens = _bigrams(query)
        rows = await self._active_rows(bot_id, 500)
        scored = [
            (_overlap(tokens, str(row.get("text") or "")), row)
            for row in rows
            if _overlap(tokens, str(row.get("text") or "")) > 0
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [dict(row) for _, row in scored[: max(1, int(limit))]]

    async def mark_recalled(self, fact_ids: list[int], now: int) -> None:
        for fact_id in fact_ids:
            row = await self.db.fetchone("SELECT * FROM facts WHERE id=?", (int(fact_id),))
            if row is None:
                continue
            if int(row.get("key_fact") or 0):
                await self.db.execute(
                    "UPDATE facts SET retrievability=1.0, last_recalled_at=? WHERE id=?",
                    (now, int(fact_id)),
                )
            else:
                r_before = self._retrievability(row, now)
                last = row.get("last_recalled_at") or row.get("valid_from") or now
                dt_days = max(0.0, (now - float(last)) / 86_400_000.0)
                strength = self.dynamics.on_recall(
                    float(row.get("storage_strength") or 0.0), r_before, dt_days
                )
                await self.db.execute(
                    "UPDATE facts SET storage_strength=?, retrievability=1.0, last_recalled_at=? "
                    "WHERE id=?",
                    (strength, now, int(fact_id)),
                )

    # ------------------------------------------------------------------ 修订
    async def supersede(self, bot_id: str, fact_id: int, now: int, by: int | None = None) -> bool:
        row = await self.db.fetchone(
            "SELECT * FROM facts WHERE id=? AND bot_id=?", (int(fact_id), bot_id)
        )
        if row is None or row.get("valid_to") is not None:
            return False
        if int(row.get("key_fact") or 0):
            return False
        await self.db.execute(
            "UPDATE facts SET valid_to=?, superseded_by=? WHERE id=?",
            (now, by, int(fact_id)),
        )
        return True

    async def decay(self, bot_id: str, now: int) -> int:
        cfg = self.configs(bot_id)
        floor = cfg.float("dynamics.archive_s", 1.0)
        rows = await self.db.fetchall(
            "SELECT * FROM facts WHERE bot_id=? AND valid_to IS NULL AND key_fact=0 AND archived=0",
            (bot_id,),
        )
        archived = 0
        for row in rows:
            strength = self.dynamics.decay(float(row.get("storage_strength") or 0.0))
            r_value = self.dynamics.r(
                strength,
                row.get("last_recalled_at") or row.get("valid_from") or now,
                now,
            )
            doomed = self.dynamics.should_archive(strength)
            await self.db.execute(
                "UPDATE facts SET storage_strength=?, retrievability=?, archived=? WHERE id=?",
                (strength, r_value, 1 if doomed else 0, int(row["id"])),
            )
            if doomed:
                archived += 1
        return archived

    async def stats(self, bot_id: str) -> dict[str, int]:
        total = await self.db.scalar(
            "SELECT COUNT(*) FROM facts WHERE bot_id=? AND archived=0", (bot_id,), default=0
        )
        active = await self.db.scalar(
            "SELECT COUNT(*) FROM facts WHERE bot_id=? AND valid_to IS NULL AND archived=0",
            (bot_id,),
            default=0,
        )
        key = await self.db.scalar(
            "SELECT COUNT(*) FROM facts WHERE bot_id=? AND key_fact=1 AND archived=0",
            (bot_id,),
            default=0,
        )
        return {"total": int(total or 0), "active": int(active or 0), "key": int(key or 0)}


def dump_sources(ids: list[int]) -> str:
    return json.dumps(sorted(set(int(value) for value in ids)), ensure_ascii=False)
