"""故事模块的对外门面：启停、暂停、手动唤醒、到期巡检、状态与导出。

预约的唤醒存在 ``story_state.next_wake_at``，由后台循环定期调用 ``tick`` 巡检。
宕机、暂停、额度耗尽都只是“空白期变长”，恢复后下一场开场时一次性补叙。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.locks import LockPool
from . import bucket
from .card import CHAT_BOUNDARY_LINE, MINUTE_MS, load_card, render_for_chat
from .director import BaseCardResolver, Director, SceneFailed, SceneProgress, SceneResult
from .llm import ROLE_ACTOR, ROLE_GM, ROLE_LABELS, StoryLLM
from .store import StoryStore
from .world import WorldSeedError, validate_seed

ConfigProvider = Callable[[str], ConfigView]


class StoryError(Exception):
    """故事模块拒绝了这次操作；消息可以直接给管理员看。"""


class StoryService:
    def __init__(
        self,
        *,
        configs: ConfigProvider,
        store: StoryStore,
        director: Director,
        llm: StoryLLM,
        audit: Audit,
        base_card: BaseCardResolver,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.configs = configs
        self.store = store
        self.director = director
        self.llm = llm
        self.audit = audit
        self._base_card = base_card
        self.now_fn = now_fn
        self._locks = LockPool()

    # ------------------------------------------------------------------ 开关
    def master_enabled(self) -> bool:
        return self.configs("").bool("story.enabled", False)

    def _lock(self, bot_id: str) -> asyncio.Lock:
        return self._locks.get("story", bot_id)

    def is_running(self, bot_id: str) -> bool:
        return self._lock(bot_id).locked()

    def _bucket_params(self, bot_id: str) -> tuple[float, float]:
        cfg = self.configs(bot_id)
        return (
            cfg.float("story.refill_per_day", 10, lo=0, hi=1000),
            cfg.float("story.bucket_capacity", 30, lo=1, hi=10000),
        )

    async def problems(self, bot_id: str) -> list[str]:
        """开启或唤醒前的前置条件；返回缺了什么。"""
        issues: list[str] = []
        if not self.master_enabled():
            issues.append("故事模块总开关没开（配置里的 story.enabled）")
        state = await self.store.state(bot_id)
        if state is None or not str(state.get("outline") or "").strip():
            issues.append("还没有导入世界种子")
        try:
            base_card, _ = await self._base_card(bot_id)
        except Exception as exc:  # noqa: BLE001
            base_card = ""
            self.audit.warn("读取基础人设失败：%s", exc)
        if not base_card.strip():
            issues.append(
                "没有取到她的基础人设（填 story.base_card，或在 AstrBot 里设置默认人格）"
            )
        for role in (ROLE_GM, ROLE_ACTOR):
            if not await self.llm.available(role, bot_id):
                issues.append(f"{ROLE_LABELS[role]}没有可用的对话模型")
        return issues

    async def import_seed(self, bot_id: str, data: Any) -> dict[str, Any]:
        seed = validate_seed(data)
        counts = await self.store.replace_seed(bot_id, seed, self.now_fn())
        self.audit.event("story_seed_imported", bot_id=bot_id, **counts)
        return {"title": seed["title"], **counts}

    async def enable(self, bot_id: str) -> dict[str, Any]:
        issues = await self.problems(bot_id)
        if issues:
            raise StoryError("；".join(issues))
        now = self.now_fn()
        state = await self.store.ensure_state(bot_id, now)
        per_day, capacity = self._bucket_params(bot_id)
        fields: dict[str, Any] = {"enabled": 1, "paused": 0, "pause_reason": None, "failures": 0}
        if not int(state.get("enabled") or 0):
            # 从关闭到开启：最多带一天的额度起步，关着的时候攒下的不算。
            start = bucket.initial(per_day=per_day, capacity=capacity)
            if int(state.get("tokens_at") or 0):
                start = min(
                    start,
                    bucket.refill(
                        float(state.get("tokens") or 0.0),
                        int(state["tokens_at"]),
                        now,
                        per_day=per_day,
                        capacity=capacity,
                    ),
                )
            fields.update(tokens=start, tokens_at=now)
        if not state.get("next_wake_at") or int(state["next_wake_at"]) < now:
            fields["next_wake_at"] = now
        await self.store.update_state(bot_id, now, **fields)
        self.audit.event("story_enabled", bot_id=bot_id)
        return await self.status(bot_id)

    async def disable(self, bot_id: str) -> dict[str, Any]:
        now = self.now_fn()
        await self.store.ensure_state(bot_id, now)
        await self.store.update_state(bot_id, now, enabled=0)
        self.audit.event("story_disabled", bot_id=bot_id)
        return await self.status(bot_id)

    async def pause(self, bot_id: str, reason: str = "手动暂停") -> dict[str, Any]:
        now = self.now_fn()
        await self.store.ensure_state(bot_id, now)
        await self.store.update_state(bot_id, now, paused=1, pause_reason=reason[:200])
        self.audit.event("story_paused", bot_id=bot_id, reason=reason)
        return await self.status(bot_id)

    async def resume(self, bot_id: str) -> dict[str, Any]:
        now = self.now_fn()
        state = await self.store.ensure_state(bot_id, now)
        fields: dict[str, Any] = {"paused": 0, "pause_reason": None, "failures": 0}
        if not state.get("next_wake_at") or int(state["next_wake_at"]) < now:
            fields["next_wake_at"] = now
        await self.store.update_state(bot_id, now, **fields)
        self.audit.event("story_resumed", bot_id=bot_id)
        return await self.status(bot_id)

    # ------------------------------------------------------------------ 唤醒
    async def check_wake(self, bot_id: str) -> None:
        """手动唤醒前的检查；不满足时抛 ``StoryError``。"""
        state = await self.store.state(bot_id)
        if state is None or not int(state.get("enabled") or 0):
            raise StoryError("这个机器人还没开启故事（先 /story on）")
        issues = await self.problems(bot_id)
        if issues:
            raise StoryError("；".join(issues))
        if self.is_running(bot_id):
            raise StoryError("这个机器人正在演一场，等它结束再试")

    async def wake(self, bot_id: str) -> SceneResult:
        """手动唤醒：不消耗额度，不受最小间隔限制，失败也不计入自动暂停。"""
        await self.check_wake(bot_id)
        async with self._lock(bot_id):
            try:
                return await self.director.run(bot_id, manual=True)
            except SceneFailed as exc:
                await self._record_failure(bot_id, exc, manual=True)
                raise StoryError(f"这一场没演成：{exc}") from exc

    async def tick(self) -> list[str]:
        """巡检到期的预约；返回这一轮演成了的机器人。"""
        if not self.master_enabled():
            return []
        played: list[str] = []
        for bot_id in await self.store.due(self.now_fn()):
            if self.is_running(bot_id):
                continue
            try:
                if await self._scheduled(bot_id):
                    played.append(bot_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单个机器人出错不影响其他
                self.audit.warn("故事巡检失败 %s：%s", bot_id, exc)
        return played

    async def _scheduled(self, bot_id: str) -> bool:
        async with self._lock(bot_id):
            now = self.now_fn()
            state = await self.store.state(bot_id)
            if (
                state is None
                or not int(state.get("enabled") or 0)
                or int(state.get("paused") or 0)
                or not state.get("next_wake_at")
                or int(state["next_wake_at"]) > now
            ):
                return False
            cfg = self.configs(bot_id)
            per_day, capacity = self._bucket_params(bot_id)
            tokens = bucket.refill(
                float(state.get("tokens") or 0.0),
                int(state.get("tokens_at") or 0),
                now,
                per_day=per_day,
                capacity=capacity,
            )
            if tokens < 1.0:
                # 额度耗尽不报错：故事静默，状态卡托底，等攒够一次再来（届时走补叙）。
                wait = bucket.ms_until(tokens, 1.0, per_day=per_day)
                if wait is None:
                    wait = cfg.int("story.max_wake_minutes", 720, lo=1, hi=10080) * MINUTE_MS
                await self.store.update_state(
                    bot_id,
                    now,
                    tokens=tokens,
                    tokens_at=now,
                    next_wake_at=now + max(wait, MINUTE_MS),
                )
                self.audit.event(
                    "story_wait_tokens", bot_id=bot_id, tokens=round(tokens, 2), wait_ms=wait
                )
                return False
            try:
                await self.director.run(bot_id, manual=False)
            except SceneFailed as exc:
                await self._record_failure(bot_id, exc, manual=False)
                return False
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 意外错误也按失败退避，不每分钟重撞
                self.audit.error("故事场景意外出错 %s：%s", bot_id, exc)
                await self._record_failure(
                    bot_id, SceneFailed(f"{type(exc).__name__}: {exc}"), manual=False
                )
                return False
            return True

    async def _record_failure(self, bot_id: str, error: SceneFailed, *, manual: bool) -> None:
        """整场丢弃：不进记忆、不扣额度；只留一条失败记录供排障。"""
        now = self.now_fn()
        progress = error.progress or SceneProgress(
            started_at=now, cause="manual" if manual else "scheduled"
        )
        await self.store.insert_scene(
            bot_id,
            cause=progress.cause,
            status="failed",
            started_at=progress.started_at,
            ended_at=now,
            rounds=progress.rounds,
            calls=progress.calls,
            seed=progress.seed or None,
            rolls=progress.rolls or None,
            transcript=progress.transcript or None,
            backfill=progress.backfill,
            error=str(error),
        )
        self.audit.warn("故事场景失败 %s：%s", bot_id, error)
        if manual:
            return
        cfg = self.configs(bot_id)
        state = await self.store.state(bot_id) or {}
        failures = int(state.get("failures") or 0) + 1
        limit = cfg.int("story.max_failures", 3, lo=1, hi=20)
        if failures >= limit:
            await self.store.update_state(
                bot_id,
                now,
                failures=failures,
                paused=1,
                pause_reason=f"连续 {failures} 次失败，已自动暂停：{error}"[:200],
            )
            self.audit.error("故事连续 %s 次失败，已自动暂停 %s", failures, bot_id)
            return
        retry = cfg.int("story.retry_minutes", 30, lo=1, hi=1440) * MINUTE_MS
        await self.store.update_state(
            bot_id, now, failures=failures, next_wake_at=now + retry
        )

    # ------------------------------------------------------------------ 聊天侧
    async def chat_lines(self, bot_id: str, now: int | None = None) -> list[str]:
        """注入聊天侧 ``# 我这会儿`` 的行：状态卡 + 只说现状、往事与打算的约束。"""
        if not self.master_enabled():
            return []
        state = await self.store.state(bot_id)
        if state is None or not int(state.get("enabled") or 0):
            return []
        cfg = self.configs(bot_id)
        lines = render_for_chat(
            load_card(state.get("card")),
            now or self.now_fn(),
            stale_hours=cfg.int("story.card_stale_hours", 6, lo=0, hi=240),
        )
        if cfg.bool("story.chat_boundary", True):
            lines.append(CHAT_BOUNDARY_LINE)
        return lines

    # ------------------------------------------------------------------ 观测
    async def status(self, bot_id: str) -> dict[str, Any]:
        now = self.now_fn()
        state = await self.store.state(bot_id) or {}
        cfg = self.configs(bot_id)
        per_day, capacity = self._bucket_params(bot_id)
        tokens = bucket.refill(
            float(state.get("tokens") or 0.0),
            int(state.get("tokens_at") or 0),
            now,
            per_day=per_day,
            capacity=capacity,
        )
        try:
            base_card, base_source = await self._base_card(bot_id)
        except Exception:  # noqa: BLE001
            base_card, base_source = "", ""
        card = load_card(state.get("card"))
        scenes = await self.store.recent_scenes(bot_id, 10, ok_only=False)
        return {
            "bot_id": bot_id,
            "master_enabled": self.master_enabled(),
            "enabled": bool(int(state.get("enabled") or 0)),
            "paused": bool(int(state.get("paused") or 0)),
            "pause_reason": state.get("pause_reason") or "",
            "running": self.is_running(bot_id),
            "has_seed": bool(str(state.get("outline") or "").strip()),
            "world_title": state.get("world_title") or "",
            "tokens": round(tokens, 2),
            "capacity": capacity,
            "refill_per_day": per_day,
            "next_wake_at": state.get("next_wake_at"),
            "last_scene_at": state.get("last_scene_at"),
            "failures": int(state.get("failures") or 0),
            "card": card.as_dict() if card else None,
            "card_lines": render_for_chat(
                card, now, stale_hours=cfg.int("story.card_stale_hours", 6, lo=0, hi=240)
            ),
            "base_card_source": base_source if base_card.strip() else "",
            "base_card_chars": len(base_card.strip()),
            "canon": await self.store.canon_counts(bot_id),
            "notes": len(await self.store.notes(bot_id)),
            "scenes_total": await self.store.scene_count(bot_id),
            # 只给她的视角（记忆碎片），不给背景模型的梗概，避免剧透
            "scenes": [
                {
                    "id": row["id"],
                    "cause": row["cause"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "rounds": row["rounds"],
                    "calls": row["calls"],
                    "salience": row.get("salience"),
                    "fragment": row.get("fragment") or "",
                    "error": row.get("error") or "",
                }
                for row in scenes
            ],
            "problems": await self.problems(bot_id),
        }

    async def export(self, bot_id: str) -> dict[str, Any]:
        return await self.store.export(bot_id)


__all__ = ["StoryError", "StoryService", "WorldSeedError"]
