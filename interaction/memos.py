"""备忘录：只做时间触发，到点把模型叫醒，由它决定说不说。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.db import Database

Scheduler = Callable[..., Awaitable[str]]
Deleter = Callable[[str], Awaitable[None]]

WAKE_INSTRUCTION = (
    "这是你之前给自己设的提醒（memo_id={memo_id}）。先用 memo 工具 action=get、memo_id={memo_id} "
    "读到完整上下文（提醒内容、现在的时间、你的人格、最近的会话），再决定要不要说话、对谁说、说什么。"
)


class MemoService:
    def __init__(
        self,
        db: Database,
        audit: Audit,
        scheduler: Scheduler,
        deleter: Deleter,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.audit = audit
        self._scheduler = scheduler
        self._deleter = deleter
        self.now_fn = now_fn

    # ------------------------------------------------------------------ 创建
    async def create(
        self,
        bot_id: str,
        *,
        note: str,
        umo: str,
        sender_id: str = "",
        run_at: str | None = None,
        cron: str | None = None,
        target_session: str = "",
    ) -> dict[str, Any]:
        body = (note or "").strip()
        if not body:
            raise ValueError("提醒内容不能为空")
        if bool(run_at) == bool(cron):
            raise ValueError("run_at 与 cron 必须二选一")
        run_at_ms: int | None = None
        run_at_dt: datetime | None = None
        if run_at:
            run_at_dt = self._parse_time(run_at)
            run_at_ms = int(run_at_dt.timestamp() * 1000)
            if run_at_ms <= self.now_fn():
                raise ValueError("run_at 必须是将来的时间")
        if cron:
            self._validate_cron(cron)

        memo_id = await self.db.execute(
            "INSERT INTO memos(bot_id, note, run_at, cron, target_session, status, job_id, "
            "fired_at, skip_reason, created_at) VALUES(?,?,?,?,?,?,NULL,NULL,NULL,?)",
            (
                bot_id,
                body[:500],
                run_at_ms,
                cron,
                target_session or umo,
                "scheduled",
                self.now_fn(),
            ),
        )
        payload = {
            "session": target_session or umo,
            "note": WAKE_INSTRUCTION.format(memo_id=memo_id),
            "sender_id": sender_id,
            "origin": "tool",
        }
        job_id = await self._scheduler(
            name=f"persona-memory-memo-{memo_id}",
            cron_expression=cron if cron else None,
            payload=payload,
            run_once=bool(run_at_ms),
            run_at=run_at_dt,
        )
        await self.db.execute("UPDATE memos SET job_id=? WHERE id=?", (job_id, memo_id))
        return {"memo_id": memo_id, "job_id": job_id, "run_at": run_at_ms, "cron": cron}

    async def list(self, bot_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            "SELECT * FROM memos WHERE bot_id=? ORDER BY id DESC LIMIT ?",
            (bot_id, max(1, int(limit))),
        )

    async def get(self, bot_id: str, memo_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM memos WHERE id=? AND bot_id=?", (int(memo_id), bot_id)
        )
        if row is None:
            return None
        if row.get("fired_at") is None:
            await self.db.execute(
                "UPDATE memos SET fired_at=?, status=CASE WHEN cron IS NULL THEN 'fired' ELSE "
                "'scheduled' END, skip_reason=NULL WHERE id=?",
                (self.now_fn(), int(memo_id)),
            )
            row["fired_at"] = self.now_fn()
            row["status"] = "fired" if row.get("cron") is None else "scheduled"
        return row

    async def skip(self, bot_id: str, memo_id: int, reason: str = "") -> None:
        await self.db.execute(
            "UPDATE memos SET skip_reason=?, status=CASE WHEN cron IS NULL THEN 'done' ELSE "
            "'scheduled' END WHERE id=? AND bot_id=?",
            (reason[:120], int(memo_id), bot_id),
        )

    async def edit(
        self,
        bot_id: str,
        memo_id: int,
        *,
        note: str | None = None,
        run_at: str | None = None,
        cron: str | None = None,
    ) -> dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT * FROM memos WHERE id=? AND bot_id=?", (int(memo_id), bot_id)
        )
        if row is None:
            raise ValueError("备忘录不存在")
        if row.get("job_id"):
            await self._deleter(str(row["job_id"]))
        body = (note or row.get("note") or "").strip()
        run_at_ms = row.get("run_at")
        cron_expr = cron if cron is not None else row.get("cron")
        run_at_dt: datetime | None = None
        if run_at:
            run_at_dt = self._parse_time(run_at)
            run_at_ms = int(run_at_dt.timestamp() * 1000)
            cron_expr = None
        elif cron:
            self._validate_cron(cron)
            run_at_ms = None
        payload = {
            "session": row.get("target_session") or "",
            "note": WAKE_INSTRUCTION.format(memo_id=int(memo_id)),
            "sender_id": "",
            "origin": "tool",
        }
        if run_at_ms:
            run_at_dt = datetime.fromtimestamp(int(run_at_ms) / 1000.0)
        job_id = await self._scheduler(
            name=f"persona-memory-memo-{memo_id}",
            cron_expression=cron_expr if not run_at_ms else None,
            payload=payload,
            run_once=bool(run_at_ms),
            run_at=run_at_dt,
        )
        await self.db.execute(
            "UPDATE memos SET note=?, run_at=?, cron=?, job_id=?, status='scheduled' WHERE id=?",
            (body[:500], run_at_ms, cron_expr, job_id, int(memo_id)),
        )
        return {"memo_id": int(memo_id), "job_id": job_id}

    async def delete(self, bot_id: str, memo_id: int) -> bool:
        row = await self.db.fetchone(
            "SELECT job_id FROM memos WHERE id=? AND bot_id=?", (int(memo_id), bot_id)
        )
        if row is None:
            return False
        if row.get("job_id"):
            try:
                await self._deleter(str(row["job_id"]))
            except Exception as exc:  # noqa: BLE001 - 任务可能已随一次任务消失
                self.audit.warn("删除备忘录任务失败：%s", exc)
        await self.db.execute("DELETE FROM memos WHERE id=?", (int(memo_id),))
        return True

    async def reconcile(self, bot_id: str, live_job_ids: set[str]) -> int:
        rows = await self.db.fetchall(
            "SELECT id, job_id FROM memos WHERE bot_id=? AND status='scheduled' AND job_id IS NOT NULL",
            (bot_id,),
        )
        lost = 0
        for row in rows:
            if str(row["job_id"]) in live_job_ids:
                continue
            await self.db.execute(
                "UPDATE memos SET status='lost' WHERE id=?", (int(row["id"]),)
            )
            lost += 1
        return lost

    async def pending(self, bot_id: str) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            "SELECT * FROM memos WHERE bot_id=? AND status='scheduled' ORDER BY id", (bot_id,)
        )

    @staticmethod
    def _parse_time(value: str) -> datetime:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"时间格式不对：{value}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed

    @staticmethod
    def _validate_cron(expression: str) -> None:
        parts = str(expression).split()
        if len(parts) != 5:
            raise ValueError("cron 必须是 5 段（分 时 日 月 周）")

    @staticmethod
    def render(row: dict[str, Any]) -> str:
        when = ""
        if row.get("run_at"):
            when = clock_mod.iso(int(row["run_at"]))
        elif row.get("cron"):
            when = f"cron {row['cron']}"
        return f"#{row.get('id')} [{row.get('status')}] {when} {row.get('note')}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
