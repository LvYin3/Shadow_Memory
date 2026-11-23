"""背景模型回想更早的场景。

最近几场的梗概每次都带；更早的场景不衰减，但也不全带：先按字面和此刻的处境粗筛，
再让判断模型一次性逐条判断“演下一场时想起它有没有用”，过阈值的才带上。
判断模型不可用或失败时，退回字面最相关的两条。

背景模型也可以自己点名去查（lookup 里写关键词或 ``#编号``），见 ``search`` 与 ``scene_text``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..memory.textutil import bigrams, overlap
from . import prompts
from .store import StoryStore

FALLBACK_ITEMS = 2
SCENE_TEXT_MAX = 900
SCENE_REF = re.compile(r"^#\s*(\d+)$")

PAST_INSTRUCTION = (
    "你是一个故事的主持人，马上要接着演下一场。下面是故事里更早的一场的梗概："
    "「{summary}」。结合此刻她的处境和最近的经过，判断：演下一场时把这一场想起来，"
    "对保持前后一致（人物关系、承诺、没收尾的事、伏笔、地点和物品）有没有用？"
    "只是同类的日常、和眼下无关的，算没用。"
)


def scene_line(row: dict[str, Any]) -> str:
    stamp = clock_mod.iso(int(row["started_at"]))[5:16].replace("T", " ")
    return f"#{row['id']} {stamp} {str(row.get('summary') or '').strip()}"


@dataclass(slots=True)
class PastPick:
    lines: list[str] = field(default_factory=list)
    reason: str = ""
    candidates: int = 0
    scores: dict[int, float] = field(default_factory=dict)
    judged: bool = False  # 用了一次判断模型调用


def _literal(rows: list[dict[str, Any]], query: str) -> list[tuple[float, dict[str, Any]]]:
    tokens = bigrams(query or "")
    scored = [(overlap(tokens, str(row.get("summary") or "")), row) for row in rows]
    return [item for item in scored if item[0] > 0]


class PastScenes:
    def __init__(self, store: StoryStore, jev: Any, audit: Audit) -> None:
        self.store = store
        self.jev = jev
        self.audit = audit

    async def pick(
        self,
        bot_id: str,
        *,
        skip: int,
        query: str,
        state: dict[str, Any],
        limit: int,
        candidates: int,
        threshold: float,
    ) -> PastPick:
        if limit <= 0:
            return PastPick(reason="off")
        rows = [
            row
            for row in await self.store.older_scenes(bot_id, skip)
            if str(row.get("summary") or "").strip()
        ]
        if not rows:
            return PastPick(reason="none")

        # 粗筛：字面相关的在前，不够再按新近补满。
        literal = sorted(_literal(rows, query), key=lambda item: -item[0])
        chosen = [row for _, row in literal[:candidates]]
        chosen_ids = {row["id"] for row in chosen}
        chosen += [row for row in rows if row["id"] not in chosen_ids][: candidates - len(chosen)]

        fallback = [row for _, row in literal[: min(limit, FALLBACK_ITEMS)]]
        if not getattr(self.jev, "available", False):
            return PastPick(
                lines=[scene_line(row) for row in fallback],
                reason="literal",
                candidates=len(chosen),
            )
        questions = {
            f"s{index}": {
                "type": "noul",
                "instructions": PAST_INSTRUCTION.format(
                    summary=str(row.get("summary") or "").strip()[:200]
                ),
            }
            for index, row in enumerate(chosen)
        }
        answers = None
        try:
            answers = await self.jev.ask(state, questions)
        except Exception as exc:  # noqa: BLE001 - 判断模型异常按失败处理
            self.audit.warn("背景模型回想旧场景失败：%s", exc)
        if not answers:
            return PastPick(
                lines=[scene_line(row) for row in fallback],
                reason="literal",
                candidates=len(chosen),
                judged=True,
            )
        scores: dict[int, float] = {}
        for index, row in enumerate(chosen):
            value = (answers.get(f"s{index}") or {}).get("noul")
            if isinstance(value, (int, float)):
                scores[int(row["id"])] = float(value)
        ranked = sorted(
            (row for row in chosen if scores.get(int(row["id"]), 0.0) >= threshold),
            key=lambda row: -scores[int(row["id"])],
        )[:limit]
        # 按时间顺序给背景模型看
        ranked.sort(key=lambda row: int(row["id"]))
        return PastPick(
            lines=[scene_line(row) for row in ranked],
            reason="judged",
            candidates=len(chosen),
            scores=scores,
            judged=True,
        )

    async def search(self, bot_id: str, words: list[str], limit: int = 3) -> list[str]:
        """背景模型点名查以前的事：关键词对场景梗概做字面匹配。"""
        if not words or limit <= 0:
            return []
        rows = [
            row
            for row in await self.store.older_scenes(bot_id, 0)
            if str(row.get("summary") or "").strip()
        ]
        hits: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            summary = str(row.get("summary") or "")
            exact = sum(1.0 for word in words if word in summary)
            score = exact + overlap(bigrams(" ".join(words)), summary)
            if exact:
                hits.append((score, row))
        hits.sort(key=lambda item: (-item[0], -int(item[1]["id"])))
        return [scene_line(row) for _, row in hits[:limit]]

    async def scene_text(self, bot_id: str, scene_id: int, style: Any = None) -> str:
        """``#编号`` 对应场景的完整经过（背景模型视角：只有她外显的言行）。"""
        row = await self.store.scene(bot_id, scene_id)
        if row is None:
            return ""
        transcript = prompts.render_transcript(row["transcript"], viewer="gm", style=style)
        backfill = str(row.get("backfill") or "").strip()
        body = (f"（补叙）{backfill}\n" if backfill else "") + transcript
        head = scene_line(row)
        return f"{head}\n{body[:SCENE_TEXT_MAX]}"
