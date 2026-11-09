"""每日衰减与归档。"""

from __future__ import annotations

from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from .dynamics import Dynamics

ConfigProvider = Callable[[str], ConfigView]


class Decayer:
    def __init__(
        self,
        db: Database,
        configs: ConfigProvider,
        dynamics: Dynamics,
        facts: Any,
        audit: Audit,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.dynamics = dynamics
        self.facts = facts
        self.audit = audit
        self.now_fn = now_fn

    async def run(self, bot_id: str, now: int | None = None) -> dict[str, int]:
        stamp = now or self.now_fn()
        rows = await self.db.fetchall(
            "SELECT id, storage_strength, last_recalled_at, ended_at, status FROM events "
            "WHERE bot_id=? AND status<>'open' AND status<>'archived'",
            (bot_id,),
        )
        archived = 0
        for row in rows:
            strength = self.dynamics.decay(float(row.get("storage_strength") or 0.0))
            r_value = self.dynamics.r(
                strength, row.get("last_recalled_at") or row.get("ended_at"), stamp
            )
            doomed = self.dynamics.should_archive(strength)
            await self.db.execute(
                "UPDATE events SET storage_strength=?, retrievability=?, status=? WHERE id=?",
                (strength, r_value, "archived" if doomed else row.get("status"), int(row["id"])),
            )
            if doomed:
                archived += 1
        facts_archived = await self.facts.decay(bot_id, stamp)
        self.audit.event(
            "decay",
            bot_id=bot_id,
            events=len(rows),
            archived=archived,
            facts_archived=facts_archived,
        )
        return {"events": len(rows), "archived": archived, "facts_archived": facts_archived}
