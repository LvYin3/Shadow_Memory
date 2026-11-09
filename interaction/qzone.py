"""QQ 空间：仅第三方框架（aiocqhttp + send_qzone_msg 后端）可用。"""

from __future__ import annotations

from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from .media_dir import MediaDir, MediaError

UGC_ALLOWED = {1, 4, 64}
UGC_LABELS = {1: "所有人", 4: "好友", 64: "仅自己"}

ConfigProvider = Callable[[str], ConfigView]
MediaProvider = Callable[[str], MediaDir]


class QzoneService:
    def __init__(
        self,
        db: Database,
        configs: ConfigProvider,
        media_provider: MediaProvider,
        audit: Audit,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.media_provider = media_provider
        self.audit = audit
        self.now_fn = now_fn

    # ------------------------------------------------------------------ 可用性
    @staticmethod
    def event_supported(event: Any) -> bool:
        return event is not None and getattr(event, "get_platform_name", lambda: "")() == (
            "aiocqhttp"
        ) and getattr(event, "bot", None) is not None

    def media(self, bot_id: str) -> MediaDir:
        cfg = self.configs(bot_id)
        return self.media_provider(bot_id)

    async def list_images(self, bot_id: str) -> list[dict[str, Any]]:
        return self.media(bot_id).list_local()

    # ------------------------------------------------------------------ 发布
    async def post(
        self,
        event: Any,
        *,
        bot_id: str,
        text: str,
        images: list[Any] | None = None,
        ugc_right: int | None = None,
        bypass_limit: bool = False,
    ) -> dict[str, Any]:
        cfg = self.configs(bot_id)
        if not cfg.bool("interaction.qzone.enabled", True):
            return {"ok": False, "message": "现在不玩空间。"}
        if not self.event_supported(event):
            await self._log(bot_id, "aiocqhttp?", 0, 0, ugc_right, None, "unsupported", "platform")
            return {"ok": False, "message": "这个平台发不了说说。"}

        body = (text or "").strip()
        if not body and not images:
            return {"ok": False, "message": "总得写点什么。"}
        if len(body) > 1000:
            return {"ok": False, "message": "太长了，说说不适合写这么长。"}

        visibility = int(ugc_right or cfg.int("interaction.qzone.ugc_right", 1))
        if visibility not in UGC_ALLOWED:
            return {"ok": False, "message": "这个可见范围不支持（只支持 1 所有人 / 4 好友 / 64 仅自己）。"}

        day = clock_mod.day_key(self.now_fn())
        if not bypass_limit:
            limit = cfg.int("interaction.qzone.daily_limit", 3, lo=0, hi=100)
            used = int(await self.db.kv_get(bot_id, f"qzone:count:{day}", "0") or 0)
            if limit and used >= limit:
                await self._log(bot_id, "aiocqhttp", len(body), len(images or []), visibility, None, "cap", "")
                return {"ok": False, "message": "今天不想再发了。"}
            if await self._disabled_today(bot_id, day):
                return {"ok": False, "message": "今天发不出去，改天吧。"}

        media = self.media(bot_id)
        try:
            refs = await media.resolve(images or [], event)
        except MediaError as exc:
            await self._log(bot_id, "aiocqhttp", len(body), 0, visibility, None, "bad_image", str(exc))
            return {"ok": False, "message": str(exc)}

        action = cfg.str("interaction.qzone.action", "send_qzone_msg") or "send_qzone_msg"
        payload: dict[str, Any] = {"content": body}
        if refs:
            payload["images"] = refs
        if visibility != 1:
            payload["ugc_right"] = visibility
        bot = getattr(event, "bot", None)
        try:
            response = await bot.call_action(action, **payload)
        except Exception as exc:  # noqa: BLE001 - 后端不支持/网络错误
            await self._bump_failure(bot_id, day)
            await self._log(
                bot_id, "aiocqhttp", len(body), len(refs), visibility, None, "failed", str(exc)[:200]
            )
            self.audit.warn("发说说失败：%s", exc)
            return {"ok": False, "message": "没发出去。"}

        tid = ""
        if isinstance(response, dict):
            tid = str(response.get("tid") or response.get("data", {}).get("tid") or "")
        await self._bump_success(bot_id, day)
        await self._log(bot_id, "aiocqhttp", len(body), len(refs), visibility, tid or None, "ok", "")
        self.audit.event("qzone_post", bot_id=bot_id, chars=len(body), images=len(refs), tid=tid)
        return {"ok": True, "tid": tid, "message": "发出去了。"}

    # ------------------------------------------------------------------ 计数
    async def _bump_success(self, bot_id: str, day: str) -> None:
        key = f"qzone:count:{day}"
        used = int(await self.db.kv_get(bot_id, key, "0") or 0) + 1
        await self.db.kv_set(bot_id, key, str(used), self.now_fn())
        await self.db.kv_set(bot_id, "qzone:fail", "0", self.now_fn())

    async def _bump_failure(self, bot_id: str, day: str) -> None:
        failures = int(await self.db.kv_get(bot_id, "qzone:fail", "0") or 0) + 1
        await self.db.kv_set(bot_id, "qzone:fail", str(failures), self.now_fn())
        if failures >= 3:
            await self.db.kv_set(bot_id, "qzone:disabled_day", day, self.now_fn())

    async def _disabled_today(self, bot_id: str, day: str) -> bool:
        return (await self.db.kv_get(bot_id, "qzone:disabled_day", "")) == day

    async def _log(
        self,
        bot_id: str,
        platform: str,
        content_len: int,
        images: int,
        ugc_right: int | None,
        tid: str | None,
        status: str,
        error: str,
    ) -> None:
        await self.db.execute(
            "INSERT INTO qzone_logs(bot_id, platform, action, content_len, images, ugc_right, tid, "
            "status, error, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                bot_id,
                platform,
                "send_qzone_msg",
                int(content_len),
                int(images),
                ugc_right,
                tid,
                status,
                error[:300],
                self.now_fn(),
            ),
        )

    async def stats(self, bot_id: str) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT status, COUNT(*) AS n FROM qzone_logs WHERE bot_id=? GROUP BY status", (bot_id,)
        )
        return {str(row["status"]): int(row["n"]) for row in rows}
