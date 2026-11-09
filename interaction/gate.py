"""群聊门控：非 @ 群消息是否邀请这个人接话。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from ..memory import prompts
from ..memory.persona import PersonaService
from ..memory.textutil import bigrams, overlap

ConfigProvider = Callable[[str], ConfigView]


@dataclass(slots=True)
class GateDecision:
    passed: bool = False
    probability: float | None = None
    threshold: float = 0.5
    reason: str = ""
    latency_ms: int = 0


class Gate:
    def __init__(
        self,
        db: Database,
        configs: ConfigProvider,
        jev: Any,
        persona: PersonaService,
        audit: Audit,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.jev = jev
        self.persona = persona
        self.audit = audit
        self.now_fn = now_fn
        self._recent: dict[str, deque[dict[str, Any]]] = {}
        self._last_reply: dict[tuple[str, str], int] = {}
        self._failures: dict[str, int] = {}
        self._backoff_until: dict[str, int] = {}

    def note_message(self, umo: str, speaker: str, text: str, ts: int | None = None) -> None:
        bucket = self._recent.get(umo)
        if bucket is None:
            bucket = deque(maxlen=60)
            self._recent[umo] = bucket
        bucket.append(
            {"speaker": speaker or "某人", "text": (text or "")[:200], "ts": ts or self.now_fn()}
        )

    def note_reply(self, bot_id: str, umo: str, ts: int | None = None) -> None:
        self._last_reply[(bot_id, umo)] = ts or self.now_fn()

    async def judge(
        self,
        *,
        bot_id: str,
        umo: str,
        message: str,
        sender_name: str,
        message_id: str = "",
        now: int | None = None,
    ) -> GateDecision:
        stamp = now or self.now_fn()
        cfg = self.configs(bot_id)
        threshold = cfg.float("interaction.gate_threshold", 0.5, lo=0.0, hi=1.0)
        if not cfg.bool("interaction.gate_enabled", True):
            return GateDecision(passed=False, threshold=threshold, reason="disabled")
        backoff = self._backoff_until.get(umo, 0)
        if stamp < backoff:
            return GateDecision(passed=False, threshold=threshold, reason="judge_failed")
        if not getattr(self.jev, "available", False):
            return GateDecision(passed=False, threshold=threshold, reason="no_provider")

        history = cfg.int("interaction.gate_history", 10, lo=1, hi=50)
        bucket = list(self._recent.get(umo) or [])
        recent = bucket[-history:]
        last_reply = self._last_reply.get((bot_id, umo))
        state = {
            "recent_messages": [
                {
                    "speaker": item["speaker"],
                    "seconds_ago": round((stamp - int(item["ts"])) / 1000.0, 1),
                    "text": item["text"],
                }
                for item in recent
            ],
            "seconds_since_last": round((stamp - int(recent[-1]["ts"])) / 1000.0, 1)
            if recent
            else None,
            "my_last_reply_seconds_ago": round((stamp - last_reply) / 1000.0, 1)
            if last_reply
            else None,
            "persona_tendencies": await self._tendencies(bot_id, message),
            "current_message": {"speaker": sender_name, "text": (message or "")[:500]},
        }
        started = clock_mod.now_ms()
        probability = await self.jev.noul(state, prompts.REPLY_NOW_INSTRUCTION)
        latency = clock_mod.now_ms() - started
        if probability is None:
            self._failures[umo] = self._failures.get(umo, 0) + 1
            if self._failures[umo] >= 3:
                self._backoff_until[umo] = stamp + 600_000
                self._failures[umo] = 0
                self.audit.warn("门控连续失败，%s 进入 10 分钟退避", umo)
            await self._log(bot_id, umo, message_id, None, threshold, "silent", "judge_failed", latency)
            return GateDecision(passed=False, threshold=threshold, reason="judge_failed", latency_ms=latency)

        self._failures[umo] = 0
        passed = probability >= threshold
        reason = "passed" if passed else "below_threshold"
        await self._log(
            bot_id,
            umo,
            message_id,
            probability,
            threshold,
            "reply" if passed else "silent",
            reason,
            latency,
        )
        return GateDecision(
            passed=passed,
            probability=probability,
            threshold=threshold,
            reason=reason,
            latency_ms=latency,
        )

    async def _tendencies(self, bot_id: str, message: str) -> list[str]:
        entries = [
            entry for entry in await self.persona.entries(bot_id) if entry.layer in {"core", "edge"}
        ]
        tokens = bigrams(message)
        entries.sort(key=lambda entry: overlap(tokens, entry.text), reverse=True)
        return [entry.text for entry in entries[:3]]

    async def _log(
        self,
        bot_id: str,
        umo: str,
        message_id: str,
        probability: float | None,
        threshold: float,
        decision: str,
        reason: str,
        latency_ms: int,
    ) -> None:
        await self.db.execute(
            "INSERT INTO gate_logs(bot_id, umo, message_id, p_reply, threshold, decision, reason, "
            "latency_ms, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                bot_id,
                umo,
                message_id,
                probability,
                threshold,
                decision,
                reason,
                int(latency_ms),
                clock_mod.now_ms(),
            ),
        )

    async def stats(self, bot_id: str) -> dict[str, Any]:
        rows = await self.db.fetchall(
            "SELECT decision, COUNT(*) AS n FROM gate_logs WHERE bot_id=? GROUP BY decision",
            (bot_id,),
        )
        return {str(row["decision"]): int(row["n"]) for row in rows}
