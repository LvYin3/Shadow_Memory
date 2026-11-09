"""记忆模块门面：把分段、S/R、边、召回、事实、人格、巩固串成一条链。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from ..core.types import Candidate, Injection, Turn
from . import prompts
from .consolidate import Consolidator
from .decay import Decayer
from .distill import Distiller
from .dynamics import Dynamics
from .edges import EdgeRepo
from .facts import FactService
from .insights import InsightRepo
from .persona import PersonaService
from .recall import RecallService
from .seed import SeedService
from .segmenter import Segmenter
from .textutil import trim_at_boundary

ConfigProvider = Callable[[str], ConfigView]


@dataclass(slots=True)
class TurnInjection:
    persona_text: str = ""
    parts: list[str] = field(default_factory=list)
    injection: Injection | None = None
    fact_ids: list[int] = field(default_factory=list)
    reason: str = ""


@dataclass(slots=True)
class PostTurn:
    salience: int = 2
    conflict: float = 0.0
    durable_fact: float = 0.0
    topic_end: float | None = None
    closed: bool = False
    tension_added: bool = False


class MemoryService:
    def __init__(
        self,
        *,
        db: Database,
        configs: ConfigProvider,
        dynamics: Dynamics,
        edges: EdgeRepo,
        persona: PersonaService,
        insights: InsightRepo,
        facts: FactService,
        segmenter: Segmenter,
        recall: RecallService,
        distiller: Distiller,
        consolidator: Consolidator,
        decayer: Decayer,
        seed: SeedService,
        jev: Any,
        textgen: Any,
        audit: Audit,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.dynamics = dynamics
        self.edges = edges
        self.persona = persona
        self.insights = insights
        self.facts = facts
        self.segmenter = segmenter
        self.recall = recall
        self.distiller = distiller
        self.consolidator = consolidator
        self.decayer = decayer
        self.seed = seed
        self.jev = jev
        self.textgen = textgen
        self.audit = audit
        self.now_fn = now_fn

    # ------------------------------------------------------------------ 观察
    async def observe(
        self, turn: Turn, *, display_name: str = "", chat_type: str = ""
    ) -> int | None:
        return await self.segmenter.observe(
            turn, display_name=display_name, chat_type=chat_type
        )

    async def close_idle(self, now: int | None = None) -> int:
        return await self.segmenter.close_idle(now or self.now_fn())

    # ------------------------------------------------------------------ 生成前
    async def prepare_turn(
        self,
        bot_id: str,
        umo: str,
        query: str,
        recent_turns: list[str],
        now: int | None = None,
        *,
        veto_reason: str | None = None,
    ) -> TurnInjection:
        stamp = now or self.now_fn()
        cfg = self.configs(bot_id)
        persona_text = await self.persona.render(bot_id)
        result = TurnInjection(persona_text=persona_text)
        if not cfg.bool("memory.enabled", True):
            result.reason = "memory_disabled"
            return result

        injection = await self.recall.decide(
            bot_id, umo, query, recent_turns, stamp, veto_reason=veto_reason
        )
        result.injection = injection
        result.reason = injection.reason
        if injection.main is not None:
            max_chars = cfg.int("recall.max_inject_chars", 40, lo=10, hi=200)
            main_text = trim_at_boundary(injection.main.fragment, max_chars)
            if main_text:
                result.parts.append(main_text)
                await self.recall.apply_recall(injection.main, stamp)
            aux = await self.recall.aux(bot_id, stamp, exclude=query)
            if aux:
                result.parts.append(aux)
            return result

        # 没想起事件时，用一条相关事实兜底（隐取通道）
        fact_rows = await self.facts.relevant(bot_id, query, stamp, limit=1)
        if fact_rows:
            text = trim_at_boundary(str(fact_rows[0].get("text") or ""), 40)
            if text:
                result.parts.append(text)
                result.fact_ids = [int(fact_rows[0]["id"])]
                await self.facts.mark_recalled(result.fact_ids, stamp)
        return result

    # ------------------------------------------------------------------ 生成后
    async def post_turn(
        self,
        *,
        bot_id: str,
        umo: str,
        user_text: str,
        assistant_text: str,
        speaker_name: str = "",
        now: int | None = None,
    ) -> PostTurn:
        stamp = now or self.now_fn()
        cfg = self.configs(bot_id)
        peer_name = (speaker_name or "").strip()[:12] or "对方"
        output = PostTurn()
        if not cfg.bool("memory.enabled", True):
            return output

        event = await self.db.fetchone(
            "SELECT id, salience FROM events WHERE bot_id=? AND umo=? AND status='open' "
            "AND kind='episodic' ORDER BY id DESC LIMIT 1",
            (bot_id, umo),
        )
        state = {
            "me": (await self.persona.render(bot_id))[:800],
            "user_message": (user_text or "")[:400],
            "my_reply": (assistant_text or "")[:400],
        }
        answers = None
        if getattr(self.jev, "available", False):
            answers = await self.jev.ask(
                state,
                {
                    "salience": {
                        "type": "score",
                        "instructions": prompts.SALIENCE_INSTRUCTION,
                        "criteria": prompts.SALIENCE_LEVELS,
                    },
                    "conflict": {
                        "type": "noul",
                        "instructions": prompts.CONFLICT_INSTRUCTION,
                    },
                    "durable_fact": {
                        "type": "noul",
                        "instructions": prompts.DURABLE_FACT_INSTRUCTION,
                    },
                    "topic_end": {
                        "type": "noul",
                        "instructions": prompts.TOPIC_END_INSTRUCTION,
                    },
                },
            )
        if answers:
            score = float(answers.get("salience", {}).get("score", 1.0))
            output.salience = min(5, max(1, int(round(score)) + 1))
            output.conflict = float(answers.get("conflict", {}).get("noul", 0.0))
            output.durable_fact = float(answers.get("durable_fact", {}).get("noul", 0.0))
            output.topic_end = float(answers.get("topic_end", {}).get("noul", 0.0))
        else:
            output.salience = 2

        if event is not None:
            await self.db.execute(
                "UPDATE events SET salience=MAX(COALESCE(salience, 0), ?) WHERE id=?",
                (output.salience, int(event["id"])),
            )

        if output.conflict >= 0.6 and event is not None:
            text = await self._draft_tension(
                bot_id, user_text, assistant_text, speaker_name=peer_name
            )
            if text:
                added = await self.persona.add_tension(
                    bot_id, text, [int(event["id"])], stamp
                )
                output.tension_added = bool(added)

        if output.durable_fact >= 0.6:
            await self.facts.enqueue(
                bot_id,
                umo,
                f"{peer_name}: {(user_text or '')[:150]}\n我: {(assistant_text or '')[:150]}",
                output.salience,
                stamp,
            )

        output.closed = await self.segmenter.maybe_close_after_turn(
            bot_id, umo, assistant_text, output.topic_end
        )
        return output

    async def rebuild_event(self, bot_id: str, event_id: int) -> dict[str, Any]:
        return await self.segmenter.rebuild_event(
            event_id, expected_bot_id=bot_id
        )

    async def _draft_tension(
        self,
        bot_id: str,
        user_text: str,
        assistant_text: str,
        *,
        speaker_name: str = "",
    ) -> str | None:
        name = (speaker_name or "").strip()[:12] or "对方"
        system = (
            "你是一个人的内心整理器。这段对话留下了一个没想明白的结，请用第一人称写一句"
            "不超过 70 字的话，说明你此刻心里悬着什么。只输出这句话。"
        )
        user = f"{name}: {user_text[:200]}\n我: {assistant_text[:200]}"
        draft = await self.textgen.generate(system, user, bot_id=bot_id, task="tension")
        text = (draft or "").strip().strip("「」\"'").split("\n")[0]
        if 4 <= len(text) <= 90:
            return text[:90]
        fallback = (user_text or assistant_text or "").strip()
        if not fallback:
            return None
        return f"我还没想明白{name}说的：{fallback[:40]}"[:90]

    # ------------------------------------------------------------------ 作业
    async def distill(self, bot_id: str, reason: str = "manual") -> Any:
        return await self.distiller.run(bot_id, reason=reason)

    async def consolidate(self, bot_id: str, now: int | None = None) -> Any:
        return await self.consolidator.run(bot_id, now)

    async def decay(self, bot_id: str, now: int | None = None) -> dict[str, int]:
        return await self.decayer.run(bot_id, now)

    async def extract_facts(self, bot_id: str, now: int | None = None) -> int:
        return await self.facts.nightly_extract(bot_id, now)

    # ------------------------------------------------------------------ 管理
    async def forget(self, bot_id: str, event_id: int) -> bool:
        event = await self.db.fetchone(
            "SELECT * FROM events WHERE id=? AND bot_id=?", (int(event_id), bot_id)
        )
        if event is None:
            return False
        async with self.db.transaction():
            if str(event.get("kind")) == "gist":
                sources = event.get("source_event_ids")
                try:
                    ids = json.loads(sources) if isinstance(sources, str) else list(sources or [])
                except (ValueError, TypeError):
                    ids = []
                for source_id in ids:
                    await self.db.execute(
                        "UPDATE events SET merged_into=NULL WHERE id=?", (int(source_id),)
                    )
            await self.db.execute("DELETE FROM events WHERE id=?", (int(event_id),))
            rows = await self.db.fetchall(
                "SELECT id, source_event_ids FROM persona_entries WHERE bot_id=? AND active=1",
                (bot_id,),
            )
            for row in rows:
                try:
                    ids = json.loads(row.get("source_event_ids") or "[]")
                except (ValueError, TypeError):
                    ids = []
                if not isinstance(ids, list):
                    continue
                remaining = [value for value in ids if int(value) != int(event_id)]
                if len(remaining) == len(ids):
                    continue
                if remaining:
                    await self.db.execute(
                        "UPDATE persona_entries SET source_event_ids=? WHERE id=?",
                        (json.dumps(remaining), int(row["id"])),
                    )
                else:
                    await self.db.execute(
                        "UPDATE persona_entries SET active=0 WHERE id=?", (int(row["id"]),)
                    )
        self.audit.event("forget", bot_id=bot_id, event_id=event_id)
        return True

    async def rollback(self, bot_id: str, version: int) -> bool:
        return await self.persona.restore(bot_id, version)

    async def import_seed(
        self, bot_id: str, data: Any, *, force: bool = False
    ) -> dict[str, int]:
        return await self.seed.import_seed(bot_id, data, force=force)

    async def export_seed(self, bot_id: str) -> dict[str, Any]:
        return await self.seed.export_seed(bot_id)

    async def export_full(self, bot_id: str) -> dict[str, Any]:
        persona_rows = await self.db.fetchall(
            "SELECT layer, text, source_event_ids FROM persona_entries WHERE bot_id=? AND active=1",
            (bot_id,),
        )
        facts = await self.facts._active_rows(bot_id, 500)  # noqa: SLF001 - 内部导出
        events = await self.db.fetchall(
            "SELECT id, kind, fragment, summary, entities, salience, storage_strength, "
            "retrievability, status, ended_at FROM events WHERE bot_id=? ORDER BY id DESC LIMIT 500",
            (bot_id,),
        )
        insights = await self.db.fetchall(
            "SELECT text, source_event_ids, used_in_version, created_at FROM insights "
            "WHERE bot_id=? ORDER BY id DESC LIMIT 500",
            (bot_id,),
        )
        notes = await self.db.fetchall(
            "SELECT kind, title, body, tags, created_at, updated_at FROM notes "
            "WHERE bot_id=? ORDER BY id DESC LIMIT 500",
            (bot_id,),
        )
        return {
            "version": "shadow_memory/full/v2",
            "bot_id": bot_id,
            "exported_at": self.now_fn(),
            "persona": [dict(row) for row in persona_rows],
            "facts": [dict(row) for row in facts],
            "events": [dict(row) for row in events],
            "insights": [dict(row) for row in insights],
            "notes": [dict(row) for row in notes],
        }

    async def restore_full(
        self,
        bot_id: str,
        data: Any,
        *,
        force: bool = False,
        backup_path: Any = None,
    ) -> dict[str, int]:
        return await self.seed.restore_full(
            bot_id, data, force=force, backup_path=backup_path
        )

    # ------------------------------------------------------------------ 观测
    async def summary(self, bot_id: str) -> dict[str, Any]:
        cfg = self.configs(bot_id)
        now = self.now_fn()
        day_ago = now - 86_400_000
        events = await self.db.fetchall(
            "SELECT status, kind, COUNT(*) AS n FROM events WHERE bot_id=? GROUP BY status, kind",
            (bot_id,),
        )
        sr = await self.db.fetchall(
            "SELECT COUNT(*) AS n, AVG(retrievability) AS r, AVG(storage_strength) AS s "
            "FROM events WHERE bot_id=? AND merged_into IS NULL",
            (bot_id,),
        )
        inject = await self.db.fetchall(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN event_ids IS NOT NULL AND event_ids<>'' THEN 1 ELSE 0 END) AS hit, "
            "AVG(p_top) AS p, SUM(COALESCE(cache_hit, 0)) AS cached, "
            "SUM(COALESCE(cache_hit, 0) + COALESCE(cache_other, 0)) AS total_tokens "
            "FROM inject_logs WHERE bot_id=? AND created_at>=?",
            (bot_id, day_ago),
        )
        gate = await self.db.fetchall(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN decision='reply' THEN 1 ELSE 0 END) AS replied "
            "FROM gate_logs WHERE bot_id=? AND created_at>=?",
            (bot_id, day_ago),
        )
        return {
            "bot_id": bot_id,
            "persona": {
                "version": await self.persona.version(bot_id),
                "chars": await self.persona.char_count(bot_id),
                "counts": await self.persona.counts(bot_id),
                "limits": {
                    "max": cfg.int("persona.max_chars", 1000),
                    "warn": cfg.int("persona.warn_chars", 1300),
                    "hard": cfg.int("persona.hard_chars", 1600),
                },
            },
            "events": events,
            "sr": sr[0] if sr else {},
            "edges": await self.edges.stats(bot_id),
            "facts": await self.facts.stats(bot_id),
            "insights_unused": await self.insights.count_unused(bot_id),
            "inject_24h": inject[0] if inject else {},
            "gate_24h": gate[0] if gate else {},
            "consolidation": await self.db.fetchall(
                "SELECT * FROM consolidation_logs WHERE bot_id=? ORDER BY id DESC LIMIT 3",
                (bot_id,),
            ),
            "distill": await self.db.fetchall(
                "SELECT * FROM distill_logs WHERE bot_id=? ORDER BY id DESC LIMIT 3", (bot_id,)
            ),
            "judge": dict(getattr(self.jev, "stats", {})),
            "text": dict(getattr(self.textgen, "stats", {})),
        }

    async def recent_injections(self, bot_id: str, umo: str, limit: int = 5) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            "SELECT event_ids, created_at FROM inject_logs WHERE bot_id=? AND umo=? "
            "ORDER BY id DESC LIMIT ?",
            (bot_id, umo, max(1, int(limit))),
        )

    async def bot_ids(self) -> list[str]:
        rows = await self.db.fetchall(
            "SELECT DISTINCT bot_id FROM events UNION SELECT DISTINCT bot_id FROM persona_entries "
            "UNION SELECT DISTINCT bot_id FROM facts"
        )
        return [str(row["bot_id"]) for row in rows if row.get("bot_id")]

    async def event_count(self, bot_id: str) -> int:
        value = await self.db.scalar(
            "SELECT COUNT(*) FROM events WHERE bot_id=?", (bot_id,), default=0
        )
        return int(value or 0)


def candidate_from_row(row: dict[str, Any], now: int) -> Candidate:
    return Candidate(
        event_id=int(row["id"]),
        fragment=str(row.get("fragment") or ""),
        kind=str(row.get("kind") or "episodic"),
        salience=int(row.get("salience") or 2),
        storage_strength=float(row.get("storage_strength") or 0.0),
        retrievability=float(row.get("retrievability") or 0.0),
        last_recalled_at=row.get("last_recalled_at"),
        ended_at=int(row.get("ended_at") or now),
    )
