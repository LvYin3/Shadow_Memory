"""夜间巩固：聚类 → gist → 事实修订 → 洞察。"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from ..core.types import ConsolidationResult
from . import prompts
from .dynamics import Dynamics
from .edges import EdgeRepo, entities_from_json, normalize_entities
from .persona import PersonaService

ConfigProvider = Callable[[str], ConfigView]


class Consolidator:
    def __init__(
        self,
        db: Database,
        configs: ConfigProvider,
        dynamics: Dynamics,
        edges: EdgeRepo,
        persona: PersonaService,
        insights: Any,
        facts: Any,
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
        self.textgen = textgen
        self.audit = audit
        self.now_fn = now_fn

    # ------------------------------------------------------------------ 聚类
    async def clusters(self, bot_id: str, now: int) -> list[list[dict[str, Any]]]:
        cfg = self.configs(bot_id)
        if not cfg.bool("consolidation.enabled", True):
            return []
        lookback = cfg.int("consolidation.lookback_days", 90, lo=1, hi=3650) * 86_400_000
        window = cfg.int("consolidation.window_days", 30, lo=1, hi=365) * 86_400_000
        min_events = cfg.int("consolidation.min_events", 3, lo=2, hi=20)
        min_entities = cfg.int("consolidation.min_entities", 2, lo=1, hi=20)
        max_events = cfg.int("consolidation.max_events_per_cluster", 5, lo=2, hi=20)
        rows = await self.db.fetchall(
            "SELECT id, fragment, summary, entities, salience, storage_strength, ended_at, "
            "last_recalled_at, umo FROM events WHERE bot_id=? AND status='closed' "
            "AND merged_into IS NULL AND kind='episodic' AND ended_at >= ? ORDER BY ended_at ASC",
            (bot_id, now - lookback),
        )
        clusters: list[list[dict[str, Any]]] = []
        entity_sets: list[set[str]] = []
        for row in rows:
            row["entities_list"] = entities_from_json(row.get("entities"))
            entity_set = set(row["entities_list"])
            best_index = -1
            best_shared = 0
            for index, cluster in enumerate(clusters):
                if len(cluster) >= max_events:
                    continue
                shared = len(entity_set & entity_sets[index])
                if shared < min_entities or shared <= best_shared:
                    continue
                start = min(int(item["ended_at"] or now) for item in cluster)
                end = max(int(item["ended_at"] or now) for item in cluster)
                span = max(end, int(row["ended_at"] or now)) - min(start, int(row["ended_at"] or now))
                if span > window:
                    continue
                best_index = index
                best_shared = shared
            if best_index >= 0:
                clusters[best_index].append(row)
                entity_sets[best_index] |= entity_set
            else:
                clusters.append([row])
                entity_sets.append(set(entity_set))

        usable = [cluster for cluster in clusters if len(cluster) >= min_events]
        r_by_id: dict[int, float] = {}
        for cluster in usable:
            for item in cluster:
                r_by_id[int(item["id"])] = self.dynamics.r(
                    float(item.get("storage_strength") or 0.0),
                    item.get("last_recalled_at") or item.get("ended_at"),
                    now,
                )
            cluster.sort(key=lambda item: r_by_id.get(int(item["id"]), 0.0))
        usable.sort(
            key=lambda cluster: sum(r_by_id.get(int(item["id"]), 0.0) for item in cluster)
            / len(cluster)
        )
        max_clusters = cfg.int("consolidation.max_clusters", 5, lo=1, hi=50)
        return [cluster[:max_events] for cluster in usable[:max_clusters]]

    # ------------------------------------------------------------------ 执行
    async def run(self, bot_id: str, now: int | None = None) -> ConsolidationResult:
        cfg = self.configs(bot_id)
        stamp = now or self.now_fn()
        result = ConsolidationResult()
        if not cfg.bool("consolidation.enabled", True):
            return result
        clusters = await self.clusters(bot_id, stamp)
        if not clusters:
            return result
        result.clusters = len(clusters)
        max_insights = cfg.int("insights.max_per_night", 2, lo=0, hi=20)
        insight_budget = max_insights
        active_facts = await self.db.fetchall(
            "SELECT id, text FROM facts WHERE bot_id=? AND valid_to IS NULL AND archived=0 "
            "ORDER BY key_fact DESC, id DESC LIMIT 30",
            (bot_id,),
        )
        facts_text = "\n".join(f"[{row['id']}] {row['text']}" for row in active_facts) or "（无）"

        for cluster in clusters:
            try:
                produced = await self._merge_cluster(
                    bot_id,
                    cluster,
                    stamp,
                    facts_text,
                    insight_budget,
                    cfg,
                )
            except Exception as exc:  # noqa: BLE001 - 单簇失败不影响其他簇
                result.failed += 1
                self.audit.warn("巩固簇失败：%s", exc)
                await self.db.execute(
                    "INSERT INTO consolidation_logs(bot_id, cluster_size, gist_id, superseded, "
                    "insights, status, created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        bot_id,
                        len(cluster),
                        None,
                        None,
                        0,
                        f"failed:{type(exc).__name__}",
                        stamp,
                    ),
                )
                continue
            if produced is None:
                result.skipped += 1
                continue
            result.merged += 1
            result.superseded += produced["superseded"]
            result.insights += produced["insights"]
            insight_budget -= produced["insights"]
        self.audit.event(
            "consolidation",
            bot_id=bot_id,
            clusters=result.clusters,
            merged=result.merged,
            superseded=result.superseded,
            insights=result.insights,
            failed=result.failed,
        )
        return result

    async def _merge_cluster(
        self,
        bot_id: str,
        cluster: list[dict[str, Any]],
        now: int,
        facts_text: str,
        insight_budget: int,
        cfg: ConfigView,
    ) -> dict[str, int] | None:
        lines: list[str] = []
        for event in cluster:
            lines.append(
                f"[事件 {event['id']}] 碎片：{event.get('fragment') or ''}\n"
                f"摘要：{event.get('summary') or '（无）'}\n"
                f"实体：{', '.join(event['entities_list']) or '（无）'}"
            )
        payload = "\n\n".join(lines)[:5000]
        proposal = await self.textgen.generate_json(
            prompts.CONSOLIDATE_SYSTEM,
            prompts.CONSOLIDATE_USER.format(events=payload, facts=facts_text),
            bot_id=bot_id,
            umo=str(cluster[0].get("umo") or ""),
            schema_hint=prompts.CONSOLIDATE_SCHEMA,
            task="consolidate",
        )
        if not isinstance(proposal, dict):
            return None

        fragment = str(proposal.get("gist_fragment") or "").strip()[:60]
        summary = str(proposal.get("gist_summary") or "").strip()[:300]
        if not fragment:
            return None
        entities = normalize_entities(proposal.get("entities")) or normalize_entities(
            [entity for event in cluster for entity in event["entities_list"]]
        )
        raw_supersede = proposal.get("supersede")
        supersede_ids = [
            int(value) for value in raw_supersede if isinstance(value, (int, float))
        ][:10] if isinstance(raw_supersede, list) else []
        raw_insights = proposal.get("insights")
        insights = [
            str(value).strip()[:80]
            for value in raw_insights
            if isinstance(value, str) and str(value).strip()
        ][: max(0, min(2, insight_budget))] if isinstance(raw_insights, list) else []

        strengths = [float(event.get("storage_strength") or 0.0) for event in cluster]
        salience = max(int(event.get("salience") or 2) for event in cluster)
        source_ids = [int(event["id"]) for event in cluster]
        started_at = min(int(event.get("ended_at") or now) for event in cluster)
        ended_at = max(int(event.get("ended_at") or now) for event in cluster)
        turn_count = int(sum(1 for _ in cluster))
        char_count = int(sum(len(str(event.get("fragment") or "")) for event in cluster))

        superseded = 0
        async with self.db.transaction():
            gist_id = await self.db.execute(
                "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, participants, "
                "turn_count, char_count, fragment, summary, entities, source_event_ids, "
                "storage_strength, retrievability, last_recalled_at, salience, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bot_id,
                    str(cluster[0].get("umo") or ""),
                    "gist",
                    started_at,
                    ended_at,
                    "[]",
                    turn_count,
                    char_count,
                    fragment,
                    summary or None,
                    json.dumps(entities, ensure_ascii=False),
                    json.dumps(source_ids, ensure_ascii=False),
                    max(strengths or [0.0]) * 1.2,
                    1.0,
                    now,
                    salience,
                    "closed",
                    now,
                ),
            )
            for event in cluster:
                await self.db.execute(
                    "UPDATE events SET merged_into=? WHERE id=?", (gist_id, int(event["id"]))
                )
            for fact_id in supersede_ids:
                if await self.facts.supersede(bot_id, fact_id, now, by=gist_id):
                    superseded += 1
            stored_insights = 0
            for text in insights:
                await self.insights.add(bot_id, text, source_ids, now)
                stored_insights += 1
            await self.db.execute(
                "INSERT INTO consolidation_logs(bot_id, cluster_size, gist_id, superseded, "
                "insights, status, created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    bot_id,
                    len(cluster),
                    gist_id,
                    json.dumps(supersede_ids, ensure_ascii=False),
                    stored_insights,
                    "ok",
                    now,
                ),
            )
        if entities:
            try:
                await self.edges.link(
                    bot_id,
                    gist_id,
                    entities,
                    now,
                    max_degree=cfg.int("edges.max_degree", 20, lo=1, hi=200),
                )
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("gist 建边失败：%s", exc)
        return {"superseded": superseded, "insights": stored_insights}
