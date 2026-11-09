"""召回：候选发现 → 判断模型单问（none + 候选）→ 注入。"""

from __future__ import annotations

from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from ..core.types import Candidate, Injection
from . import prompts
from .dynamics import Dynamics
from .edges import EdgeRepo, entities_from_json
from .persona import PersonaService
from .textutil import bigrams, overlap, trim_at_boundary

ConfigProvider = Callable[[str], ConfigView]


class RecallService:
    def __init__(
        self,
        db: Database,
        configs: ConfigProvider,
        dynamics: Dynamics,
        edges: EdgeRepo,
        jev: Any,
        persona: PersonaService,
        audit: Audit,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.dynamics = dynamics
        self.edges = edges
        self.jev = jev
        self.persona = persona
        self.audit = audit
        self.now_fn = now_fn
        self._floor_hits = 0

    # ------------------------------------------------------------------ 候选
    async def candidates(
        self, bot_id: str, query: str, now: int
    ) -> tuple[list[Candidate], int]:
        cfg = self.configs(bot_id)
        limit = cfg.int("recall.max_candidates", 12, lo=1, hi=50)
        floor = cfg.float("dynamics.r_floor", 0.2)
        rows = await self.db.fetchall(
            "SELECT * FROM events WHERE bot_id=? AND status='closed' AND merged_into IS NULL "
            "AND kind IN ('episodic', 'gist') ORDER BY ended_at DESC LIMIT 200",
            (bot_id,),
        )
        tokens = bigrams(query)
        scored: list[tuple[float, dict[str, Any], float]] = []
        below_floor = 0
        for row in rows:
            r_value = self.dynamics.r(
                float(row.get("storage_strength") or 0.0),
                row.get("last_recalled_at") or row.get("ended_at"),
                now,
            )
            if r_value < floor:
                below_floor += 1
                continue
            fragment = str(row.get("fragment") or "")
            score = overlap(tokens, fragment)
            for entity in entities_from_json(row.get("entities")):
                if entity and entity in (query or "").lower():
                    score += 0.35
            if score <= 0:
                continue
            scored.append((score * (0.5 + r_value), row, r_value))

        self._floor_hits = below_floor
        if not scored:
            return [], below_floor
        scored.sort(key=lambda item: item[0], reverse=True)

        max_neighbors = cfg.int("edges.neighbors", 4, lo=0, hi=20)
        base_count = max(1, limit - max_neighbors)
        base = scored[:base_count]
        selected: dict[int, tuple[float, dict[str, Any], float, float]] = {}
        for score, row, r_value in base:
            selected[int(row["id"])] = (score, row, r_value, 1.0)

        if cfg.bool("edges.enabled", True) and max_neighbors > 0:
            for _, row, _ in base:
                try:
                    neighbors = await self.edges.neighbors(
                        bot_id, int(row["id"]), max_neighbors
                    )
                except Exception as exc:  # noqa: BLE001
                    self.audit.warn("邻居查询失败：%s", exc)
                    continue
                for item in neighbors:
                    neighbor_id = int(item["event_id"])
                    if neighbor_id in selected:
                        weight = float(item.get("weight") or 0)
                        current = selected[neighbor_id]
                        selected[neighbor_id] = (
                            max(current[0], 0.2 * weight),
                            current[1],
                            current[2],
                            max(current[3], weight),
                        )
                        continue
                    neighbor = await self.db.fetchone(
                        "SELECT * FROM events WHERE id=? AND bot_id=? AND status='closed' "
                        "AND merged_into IS NULL",
                        (neighbor_id, bot_id),
                    )
                    if neighbor is None:
                        continue
                    r_value = self.dynamics.r(
                        float(neighbor.get("storage_strength") or 0.0),
                        neighbor.get("last_recalled_at") or neighbor.get("ended_at"),
                        now,
                    )
                    if r_value < floor:
                        continue
                    selected[neighbor_id] = (
                        0.2 * float(item.get("weight") or 0.0),
                        neighbor,
                        r_value,
                        float(item.get("weight") or 1.0),
                    )

        ids = list(selected.keys())
        degrees = await self.edges.degree(bot_id, ids) if ids else {}
        max_weight = max((value[3] for value in selected.values()), default=1.0) or 1.0
        candidates: list[Candidate] = []
        for event_id, (_, row, r_value, weight) in selected.items():
            salience = int(row.get("salience") or 2)
            final = r_value * (0.5 + 0.1 * salience) + 0.2 * (weight / max_weight)
            candidates.append(
                Candidate(
                    event_id=event_id,
                    fragment=str(row.get("fragment") or ""),
                    kind=str(row.get("kind") or "episodic"),
                    salience=salience,
                    storage_strength=float(row.get("storage_strength") or 0.0),
                    retrievability=r_value,
                    last_recalled_at=row.get("last_recalled_at"),
                    ended_at=int(row.get("ended_at") or now),
                    degree=int(degrees.get(event_id, 0)),
                    edge_weight=float(weight),
                    score=final,
                )
            )
        candidates.sort(key=lambda item: item.score, reverse=True)
        return candidates[:limit], below_floor

    # ------------------------------------------------------------------ 决定
    async def decide(
        self,
        bot_id: str,
        umo: str,
        query: str,
        recent_turns: list[str],
        now: int,
        *,
        veto_reason: str | None = None,
    ) -> Injection:
        cfg = self.configs(bot_id)
        if veto_reason:
            return Injection(reason=veto_reason)
        candidates, below_floor = await self.candidates(bot_id, query, now)
        if not candidates:
            return Injection(
                reason="r_below_floor" if below_floor else "none_wins",
                candidates=0,
            )
        if not getattr(self.jev, "available", False):
            return Injection(reason="no_provider", candidates=len(candidates))

        options = {"none": "此刻什么也没想起来"}
        for index, candidate in enumerate(candidates):
            options[f"c{index}"] = trim_at_boundary(candidate.fragment, 60)

        state = await self._build_state(bot_id, umo, query, recent_turns, candidates, now)
        probabilities = await self.jev.choice(state, prompts.RECALL_INSTRUCTION, options)
        if not probabilities:
            return Injection(reason="judge_failed", candidates=len(candidates))

        p_none = float(probabilities.get("none", 0.0))
        best_key = "none"
        best_value = p_none
        for index in range(len(candidates)):
            key = f"c{index}"
            value = float(probabilities.get(key, 0.0))
            if value > best_value:
                best_key, best_value = key, value
        if best_key == "none":
            return Injection(
                reason="none_wins", p_none=p_none, p_top=best_value, candidates=len(candidates)
            )
        threshold = cfg.float("recall.theta_pre", 0.40, lo=0.0, hi=1.0)
        if best_value < threshold:
            return Injection(
                reason="below_threshold",
                p_none=p_none,
                p_top=best_value,
                candidates=len(candidates),
            )
        index = int(best_key[1:])
        return Injection(
            main=candidates[index],
            reason="injected",
            p_none=p_none,
            p_top=best_value,
            candidates=len(candidates),
        )

    async def _build_state(
        self,
        bot_id: str,
        umo: str,
        query: str,
        recent_turns: list[str],
        candidates: list[Candidate],
        now: int,
    ) -> dict[str, Any]:
        entries = await self.persona.entries(bot_id)
        tensions = [entry.text for entry in entries if entry.layer == "tension"][:4]
        tokens = bigrams(query)
        tendencies = sorted(
            (entry for entry in entries if entry.layer in {"core", "edge"}),
            key=lambda entry: overlap(tokens, entry.text),
            reverse=True,
        )
        session = await self.db.fetchone(
            "SELECT last_message_at FROM sessions WHERE bot_id=? AND umo=?", (bot_id, umo)
        )
        hours = 0.0
        if session and session.get("last_message_at"):
            hours = max(
                0.0, (now - float(session["last_message_at"])) / 3_600_000.0
            )
        return {
            "persona_tensions": tensions,
            "persona_tendencies": [entry.text for entry in tendencies[:3]],
            "recent_turns": recent_turns[-4:],
            "hours_since_last_session": round(hours, 2),
            "current_message": query,
            "candidates": [
                {
                    "id": candidate.event_id,
                    "fragment": trim_at_boundary(candidate.fragment, 60),
                    "age_days": round((now - candidate.ended_at) / 86_400_000.0, 1),
                    "r": round(candidate.retrievability, 3),
                    "s": round(candidate.storage_strength, 2),
                    "degree": candidate.degree,
                }
                for candidate in candidates
            ],
        }

    # ------------------------------------------------------------------ 辅句
    async def aux(self, bot_id: str, now: int, *, exclude: str | None = None) -> str | None:
        cfg = self.configs(bot_id)
        entries = [entry for entry in await self.persona.entries(bot_id) if entry.layer == "tension"]
        if not entries:
            return None
        if not getattr(self.jev, "available", False):
            return None
        options = {"none": "此刻没有余波"}
        for index, entry in enumerate(entries[:4]):
            options[f"t{index}"] = entry.text[:70]
        state = {
            "persona_tensions": [entry.text for entry in entries[:4]],
            "current_moment": (exclude or "")[:200],
        }
        probabilities = await self.jev.choice(state, prompts.AUX_INSTRUCTION, options)
        if not probabilities:
            return None
        p_none = float(probabilities.get("none", 0.0))
        best_key = "none"
        best_value = p_none
        for index in range(len(entries[:4])):
            key = f"t{index}"
            value = float(probabilities.get(key, 0.0))
            if value > best_value:
                best_key, best_value = key, value
        threshold = cfg.float("recall.theta_aux", 0.35, lo=0.0, hi=1.0)
        if best_key == "none" or best_value < threshold:
            return None
        return trim_at_boundary(entries[int(best_key[1:])].text, 40)

    # ------------------------------------------------------------------ 召回更新
    async def apply_recall(self, candidate: Candidate, now: int) -> None:
        row = await self.db.fetchone("SELECT * FROM events WHERE id=?", (candidate.event_id,))
        if row is None:
            return
        last = row.get("last_recalled_at") or row.get("ended_at") or now
        dt_days = max(0.0, (now - float(last)) / 86_400_000.0)
        r_before = self.dynamics.r(float(row.get("storage_strength") or 0.0), last, now)
        strength = self.dynamics.on_recall(
            float(row.get("storage_strength") or 0.0), r_before, dt_days
        )
        await self.db.execute(
            "UPDATE events SET storage_strength=?, retrievability=1.0, last_recalled_at=?, "
            "last_injected_at=? WHERE id=?",
            (strength, now, now, candidate.event_id),
        )
