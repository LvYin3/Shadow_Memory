"""生图模块对外门面：配额、并发锁、拍照、自动参考图、提供商测试与日志。"""

from __future__ import annotations

import time
from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from ..core.locks import LockPool
from .pipeline import PhotoPipeline, PipelineError
from .providers import (
    ImageProviderClient,
    ProviderError,
    normalize_provider,
    pick_provider,
)
from .refs import RefError, RefStore, normalize_ref_category
from .temp import PhotoTempStore

ConfigProvider = Callable[[str], ConfigView]


class ImageError(ValueError):
    """生图服务拒绝执行。"""


class ImageService:
    def __init__(
        self,
        *,
        db: Database,
        configs: ConfigProvider,
        refs: RefStore,
        temp: PhotoTempStore,
        client: ImageProviderClient,
        pipeline: PhotoPipeline,
        audit: Audit,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.refs = refs
        self.temp = temp
        self.client = client
        self.pipeline = pipeline
        self.audit = audit
        self.now_fn = now_fn
        self._locks = LockPool()

    def enabled(self, bot_id: str = "") -> bool:
        return self.configs(bot_id).bool("image.enabled", False)

    def resolve_provider(
        self, bot_id: str = "", provider_id: str = ""
    ) -> dict[str, Any] | None:
        cfg = self.configs(bot_id)
        providers = cfg.get("image.providers", [])
        if not isinstance(providers, list):
            providers = []
        chosen_id = (provider_id or "").strip() or cfg.str("image.provider_id", "")
        return pick_provider(providers, chosen_id)

    # ------------------------------------------------------------------ 每日配额
    async def daily_usage(self, bot_id: str) -> tuple[int, int]:
        day = clock_mod.day_key(self.now_fn())
        used = int(await self.db.kv_get(bot_id, f"image:count:{day}", "0") or 0)
        limit = self.configs(bot_id).int("image.daily_limit", 20, lo=0, hi=1000)
        return used, limit

    async def _consume_quota(self, bot_id: str) -> bool:
        day = clock_mod.day_key(self.now_fn())
        limit = self.configs(bot_id).int("image.daily_limit", 20, lo=0, hi=1000)
        key = f"image:count:{day}"
        used = int(await self.db.kv_get(bot_id, key, "0") or 0)
        if limit > 0 and used >= limit:
            return False
        await self.db.kv_set(bot_id, key, str(used + 1), self.now_fn())
        return True

    # ------------------------------------------------------------------ 拍照
    async def take_photo(
        self,
        *,
        bot_id: str,
        scene: str,
        umo: str = "",
        kind: str = "photo_chat",
        recent_chat: str = "",
        story_live: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cfg = self.configs(bot_id)
        if not cfg.bool("image.enabled", False):
            return {"ok": False, "code": "disabled", "message": "现在没开拍照功能。"}
        desc = (scene or "").strip()
        if not desc:
            return {"ok": False, "code": "empty", "message": "得先说清楚想拍什么画面。"}
        provider = self.resolve_provider(bot_id)
        if provider is None:
            return {
                "ok": False,
                "code": "no_provider",
                "message": "还没配好拍照用的生图提供商。",
            }

        async with self._locks.get("image", bot_id):
            started = time.monotonic()
            try:
                out = await self.pipeline.run_photo(
                    bot_id=bot_id,
                    provider=provider,
                    scene_request=desc[:600],
                    umo=umo,
                    recent_chat=recent_chat,
                    story_live=story_live,
                    consume_quota=self._consume_quota,
                )
            except PipelineError as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                await self._log(
                    bot_id,
                    kind=kind,
                    provider_id=provider["id"],
                    protocol=provider["protocol"],
                    ref_count=0,
                    retries=exc.retries,
                    review_status=exc.code,
                    status="failed",
                    error=str(exc),
                    latency_ms=latency_ms,
                )
                if exc.code == "quota":
                    msg = "今天拍得够多了，先歇会儿吧。"
                else:
                    msg = "手抖没拍好，这张糊了没留下。"
                return {"ok": False, "code": exc.code, "message": msg, "error": str(exc)}

            staged = await self.temp.save(
                bot_id,
                out.image.data,
                prompt=out.prompt,
                description=out.description,
            )
            await self._log(
                bot_id,
                kind=kind,
                provider_id=provider["id"],
                protocol=provider["protocol"],
                ref_count=out.ref_count,
                retries=out.retries,
                review_status=out.review_status,
                status="ok",
                error="",
                latency_ms=out.latency_ms,
            )
            self.audit.event(
                "image_photo_ok",
                bot_id=bot_id,
                source_kind=kind,
                handle=staged["handle"],
                refs=out.ref_count,
                retries=out.retries,
                latency_ms=out.latency_ms,
            )
            return_desc = cfg.bool("image.return_description", False)
            msg = f"拍好了：{staged['handle']}（可直接用 send_image / qzone / media(save) 引用）"
            if return_desc and out.description:
                msg += f"\n画面：{out.description}"
            return {
                "ok": True,
                "photo_id": staged["id"],
                "handle": staged["handle"],
                "data": out.image.data,
                "mime": out.image.mime,
                "prompt": out.prompt,
                "description": out.description if return_desc else "",
                "raw_description": out.description,
                "message": msg,
            }

    async def test_photo(
        self,
        *,
        bot_id: str,
        scene: str,
        provider_id: str = "",
        include_story: bool | None = None,
        recent_chat: str = "",
        run_review: bool = False,
    ) -> dict[str, Any]:
        """WebUI 生图测试：忽略总开关与每日配额，纯内存返回原图与调试信息，不落盘。"""
        desc = (scene or "").strip()
        if not desc:
            return {"ok": False, "code": "empty", "error": "请输入原始画面描述"}
        provider = self.resolve_provider(bot_id, provider_id=provider_id)
        if provider is None:
            return {
                "ok": False,
                "code": "no_provider",
                "error": "未找到可用的生图提供商，请先在生图页配置并保存提供商档案",
            }

        async with self._locks.get("image", bot_id):
            started = time.monotonic()
            try:
                out = await self.pipeline.run_photo_test(
                    bot_id=bot_id,
                    provider=provider,
                    scene_request=desc[:800],
                    include_story=include_story,
                    recent_chat=(recent_chat or "")[:1200],
                    run_review=bool(run_review),
                )
            except PipelineError as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                await self._log(
                    bot_id,
                    kind="webui_test",
                    provider_id=provider["id"],
                    protocol=provider["protocol"],
                    ref_count=0,
                    retries=0,
                    review_status=exc.code,
                    status="failed",
                    error=str(exc),
                    latency_ms=latency_ms,
                )
                return {"ok": False, "code": exc.code, "error": str(exc)}

            review_status = (
                str(out.review.get("status") or "skipped")
                if isinstance(out.review, dict)
                else "skipped"
            )
            await self._log(
                bot_id,
                kind="webui_test",
                provider_id=provider["id"],
                protocol=provider["protocol"],
                ref_count=len(out.refs),
                retries=0,
                review_status=review_status,
                status="ok",
                error="",
                latency_ms=out.latency_ms,
            )
            self.audit.event(
                "image_webui_test_ok",
                bot_id=bot_id,
                provider_id=provider["id"],
                refs=len(out.refs),
                review_status=review_status,
                latency_ms=out.latency_ms,
            )
            return {
                "ok": True,
                "data": out.image.data,
                "mime": out.image.mime,
                "ext": out.image.ext,
                "size": len(out.image.data),
                "provider_id": provider["id"],
                "provider_name": provider.get("name") or provider["id"],
                "protocol": provider["protocol"],
                "prompt": out.prompt,
                "aspect_ratio": out.aspect_ratio,
                "refs": out.refs,
                "context_text": out.context_text,
                "review": out.review,
                "translate_meta": out.translate_meta,
                "latency_ms": out.latency_ms,
            }

    # ------------------------------------------------------------------ 自动参考图
    async def generate_auto_ref(
        self,
        *,
        bot_id: str,
        category: str,
        name: str,
        appearance: str = "",
        scene_id: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        cfg = self.configs(bot_id)
        if not cfg.bool("image.enabled", False):
            return {"ok": False, "code": "disabled", "message": "生图功能未开启"}
        if not force and not cfg.bool("image.ref.auto_generate", True):
            return {"ok": False, "code": "auto_off", "message": "未开启自动生成参考图"}
        try:
            cat = normalize_ref_category(category)
        except RefError as exc:
            return {"ok": False, "code": "bad_category", "message": str(exc)}
        allowed, reason = await self.refs.can_auto_generate(bot_id, cat, name)
        if not allowed:
            return {"ok": False, "code": "skipped", "message": reason}
        provider = self.resolve_provider(bot_id)
        if provider is None:
            return {"ok": False, "code": "no_provider", "message": "未配置可用生图提供商"}

        async with self._locks.get("image", bot_id):
            allowed, reason = await self.refs.can_auto_generate(bot_id, cat, name)
            if not allowed:
                return {"ok": False, "code": "skipped", "message": reason}
            started = time.monotonic()
            try:
                out = await self.pipeline.run_auto_ref(
                    bot_id=bot_id,
                    provider=provider,
                    category=cat,
                    name=name,
                    appearance=appearance,
                    consume_quota=self._consume_quota,
                )
            except PipelineError as exc:
                await self._log(
                    bot_id,
                    kind="auto_ref",
                    provider_id=provider["id"],
                    protocol=provider["protocol"],
                    ref_count=0,
                    retries=0,
                    review_status=exc.code,
                    status="failed",
                    error=str(exc),
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                return {"ok": False, "code": exc.code, "message": str(exc)}

            require_review = cfg.bool("image.ref.require_review", True)
            try:
                if require_review:
                    saved = await self.refs.add_pending(
                        bot_id,
                        category=cat,
                        name=name,
                        data=out.image.data,
                        description=out.description or appearance,
                        prompt=out.prompt,
                        scene_id=scene_id,
                    )
                else:
                    saved = await self.refs.add_active(
                        bot_id,
                        category=cat,
                        name=name,
                        data=out.image.data,
                        description=out.description or appearance,
                        source="auto",
                        prompt=out.prompt,
                        scene_id=scene_id,
                    )
            except RefError as exc:
                return {"ok": False, "code": "store_rejected", "message": str(exc)}

            await self._log(
                bot_id,
                kind="auto_ref",
                provider_id=provider["id"],
                protocol=provider["protocol"],
                ref_count=out.ref_count,
                retries=0,
                review_status=out.review_status,
                status="ok",
                error="",
                latency_ms=out.latency_ms,
            )
            self.audit.event(
                "image_auto_ref",
                bot_id=bot_id,
                category=cat,
                name=name,
                status=saved["status"],
                ref_id=saved["id"],
            )
            return {"ok": True, "item": saved}

    # ------------------------------------------------------------------ 提供商测试与清理
    async def test_provider(self, provider_raw: dict[str, Any]) -> dict[str, Any]:
        provider = normalize_provider(provider_raw)
        started = time.monotonic()
        try:
            generated = await self.client.generate(
                provider,
                "A simple clean cup of coffee on a wooden desk, natural daylight, minimal photograph",
                [],
                max_bytes=8 * 1024 * 1024,
            )
        except ProviderError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            await self._log(
                "",
                kind="test",
                provider_id=provider["id"],
                protocol=provider["protocol"],
                ref_count=0,
                retries=0,
                review_status="skipped",
                status="failed",
                error=str(exc),
                latency_ms=latency_ms,
            )
            return {"ok": False, "latency_ms": latency_ms, "error": str(exc)}
        latency_ms = int((time.monotonic() - started) * 1000)
        await self._log(
            "",
            kind="test",
            provider_id=provider["id"],
            protocol=provider["protocol"],
            ref_count=0,
            retries=0,
            review_status="skipped",
            status="ok",
            error="",
            latency_ms=latency_ms,
        )
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "mime": generated.mime,
            "size": len(generated.data),
        }

    async def cleanup_temp(self) -> int:
        return await self.temp.cleanup()

    async def _log(
        self,
        bot_id: str,
        *,
        kind: str,
        provider_id: str,
        protocol: str,
        ref_count: int,
        retries: int,
        review_status: str,
        status: str,
        error: str,
        latency_ms: int,
    ) -> None:
        await self.db.execute(
            "INSERT INTO image_logs(bot_id, kind, provider_id, protocol, ref_count, retries, "
            "review_status, status, error, latency_ms, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                bot_id,
                kind,
                provider_id or None,
                protocol or None,
                int(ref_count),
                int(retries),
                review_status or None,
                status,
                (error or "")[:300] or None,
                int(latency_ms),
                self.now_fn(),
            ),
        )
