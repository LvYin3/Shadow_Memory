"""人格蒸馏：快照 → 提议 → 规则裁决 → 写入。"""

from __future__ import annotations

from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from ..core.types import DistillResult
from . import prompts
from .insights import InsightRepo
from .persona import PersonaService

ConfigProvider = Callable[[str], ConfigView]


class Distiller:
    def __init__(
        self,
        db: Database,
        configs: ConfigProvider,
        persona: PersonaService,
        insights: InsightRepo,
        textgen: Any,
        audit: Audit,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.persona = persona
        self.insights = insights
        self.textgen = textgen
        self.audit = audit
        self.now_fn = now_fn

    async def last_cycle(self, bot_id: str) -> int:
        value = await self.db.scalar(
            "SELECT MAX(cycle_at) FROM distill_logs WHERE bot_id=?", (bot_id,), default=0
        )
        return int(value or 0)

    async def due(self, bot_id: str, now: int | None = None) -> tuple[bool, str]:
        cfg = self.configs(bot_id)
        stamp = now or self.now_fn()
        last = await self.last_cycle(bot_id)
        baseline = last
        if not last:
            first_event = await self.db.scalar(
                "SELECT MIN(created_at) FROM events WHERE bot_id=?", (bot_id,), default=0
            )
            baseline = int(first_event or stamp) - 1
        gist_count = int(
            await self.db.scalar(
                "SELECT COUNT(*) FROM events WHERE bot_id=? AND kind='gist' AND created_at > ?",
                (bot_id, baseline),
                default=0,
            )
            or 0
        )
        days = (stamp - baseline) / 86_400_000.0 if baseline > 0 else 0.0
        if days >= cfg.int("persona.distill_days", 7, lo=1, hi=365):
            return True, f"{days:.1f} 天未蒸馏"
        if gist_count >= cfg.int("persona.distill_events", 20, lo=1, hi=1000):
            return True, f"新增 {gist_count} 条 gist"
        return False, ""

    async def run(
        self, bot_id: str, *, reason: str = "scheduled", now: int | None = None
    ) -> DistillResult:
        cfg = self.configs(bot_id)
        stamp = now or self.now_fn()
        last = await self.last_cycle(bot_id)
        gists = await self.db.fetchall(
            "SELECT id, fragment, summary, salience FROM events WHERE bot_id=? AND kind='gist' "
            "AND created_at > ? ORDER BY id DESC LIMIT 40",
            (bot_id, last),
        )
        insights = await self.insights.unused(bot_id, 20)
        entries = await self.persona.entries(bot_id)
        tensions = [entry for entry in entries if entry.layer == "tension"]
        if not gists and not insights and not tensions:
            return DistillResult(skipped="no_sources")

        persona_text = await self.persona.render(bot_id)
        counts = await self.persona.counts(bot_id)
        source_lines: list[str] = []
        for row in gists:
            source_lines.append(
                f"[gist {row['id']}] {row.get('fragment') or ''} / {row.get('summary') or ''}"
            )
        for row in insights:
            source_lines.append(f"[insight {row['id']}] {row.get('text') or ''}")
        for entry in tensions:
            source_lines.append(f"[tension {entry.id}] {entry.text}")
        sources = "\n".join(source_lines)[:6000]

        result = await self.textgen.generate_json(
            prompts.DISTILL_SYSTEM,
            prompts.DISTILL_USER.format(
                chars=len(persona_text),
                limit=cfg.int("persona.hard_chars", 1600),
                persona=persona_text or "（空）",
                core=counts.get("core", 0),
                edge=counts.get("edge", 0),
                tension=counts.get("tension", 0),
                core_q=cfg.int("persona.quota_core", 6),
                edge_q=cfg.int("persona.quota_edge", 7),
                tension_q=cfg.int("persona.quota_tension", 3),
                sources=sources,
            ),
            bot_id=bot_id,
            schema_hint=prompts.DISTILL_SCHEMA,
            task="distill",
        )
        if not isinstance(result, dict):
            return DistillResult(skipped="text_unavailable")

        raw_ops = result.get("ops")
        ops = [item for item in raw_ops if isinstance(item, dict)] if isinstance(raw_ops, list) else []
        if not ops:
            await self._log(bot_id, stamp, len(gists), len(insights), 0, 0, 0, "no_ops")
            return DistillResult(skipped="no_ops")

        snapshot_id = await self.persona.snapshot(bot_id, f"before_distill:{reason}")
        outcome = await self.persona.apply_ops(bot_id, ops, now=stamp)
        if outcome.applied:
            await self.insights.mark_used(
                bot_id, snapshot_id, [int(row["id"]) for row in insights]
            )
        await self._log(
            bot_id,
            stamp,
            len(gists),
            len(insights),
            len(ops),
            outcome.applied,
            len(outcome.rejected),
            reason if outcome.applied else "rejected",
        )
        self.audit.event(
            "distill",
            bot_id=bot_id,
            reason=reason,
            proposed=len(ops),
            applied=outcome.applied,
            rejected=len(outcome.rejected),
            version=snapshot_id,
        )
        return DistillResult(applied=outcome.applied, rejected=outcome.rejected)

    async def _log(
        self,
        bot_id: str,
        stamp: int,
        gist_count: int,
        insight_count: int,
        proposed: int,
        applied: int,
        rejected: int,
        reason: str,
    ) -> None:
        await self.db.execute(
            "INSERT INTO distill_logs(bot_id, cycle_at, gist_count, insight_count, proposed, "
            "applied, rejected, reason, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                bot_id,
                stamp,
                gist_count,
                insight_count,
                proposed,
                applied,
                rejected,
                reason,
                stamp,
            ),
        )
