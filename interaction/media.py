"""本地图片库：存图、查图、删图、发图取数。"""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import urlparse

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import DEFAULT_VISION_PROMPT, ConfigView
from ..core.db import Database
from ..core.http import HTTPClient
from ..memory.textutil import bigrams, overlap
from .media_dir import IMAGE_MIME_HINTS, MediaDir, MediaError, human_size, sniff_image_ext
from .media_fetch import MediaFetchError, download_image, ensure_public_url

ConfigProvider = Callable[[str], ConfigView]
DirProvider = Callable[[str], MediaDir]
Captioner = Callable[..., Any]

DAY = 86_400_000


def _clean_tags(values: Any, limit: int = 8) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    tags: list[str] = []
    for value in values:
        text = str(value or "").strip()[:16]
        if text and text not in tags:
            tags.append(text)
        if len(tags) >= limit:
            break
    return tags


def _row_out(row: dict[str, Any]) -> dict[str, Any]:
    try:
        tags = json.loads(row.get("tags") or "[]")
    except (ValueError, TypeError):
        tags = []
    return {
        "filename": row.get("filename") or "",
        "description": row.get("description") or "",
        "tags": tags if isinstance(tags, list) else [],
        "size": int(row.get("size") or 0),
        "size_kb": round(int(row.get("size") or 0) / 1024.0, 1),
        "created_at": int(row.get("created_at") or 0),
        "sender_name": row.get("sender_name") or "",
        "use_count": int(row.get("use_count") or 0),
    }


class MediaService:
    def __init__(
        self,
        db: Database,
        configs: ConfigProvider,
        dir_provider: DirProvider,
        http: HTTPClient,
        audit: Audit,
        now_fn: Callable[[], int] = clock_mod.now_ms,
        url_validator: Callable[[str], None] = ensure_public_url,
        captioner: Captioner | None = None,
    ) -> None:
        self.db = db
        self.configs = configs
        self.dir_provider = dir_provider
        self.http = http
        self.audit = audit
        self.now_fn = now_fn
        self.url_validator = url_validator
        self.captioner = captioner

    def media(self, bot_id: str) -> MediaDir:
        return self.dir_provider(bot_id)

    # ------------------------------------------------------------------ 存图
    async def save(
        self,
        event: Any,
        *,
        bot_id: str,
        source: str,
        name: str = "",
        description: str = "",
        tags: Any = None,
    ) -> dict[str, Any]:
        cfg = self.configs(bot_id)
        if not cfg.bool("interaction.media.enabled", True):
            return {"ok": False, "message": "现在不收图。"}
        media = self.media(bot_id)
        source_text = str(source or "").strip()
        sender_id = sender_name = source_umo = ""
        if source_text.startswith("event:"):
            try:
                index = int(source_text.split(":", 1)[1])
            except (ValueError, IndexError):
                return {"ok": False, "message": "event 引用要写成 event:1 这样。"}
            try:
                data = await media.read_event_image(event, index)
            except MediaError as exc:
                return {"ok": False, "message": str(exc)}
            kind, source_ref = "event", source_text
            sender_id = getattr(event, "get_sender_id", lambda: "")() or ""
            sender_name = getattr(event, "get_sender_name", lambda: "")() or ""
            source_umo = getattr(event, "unified_msg_origin", "") or ""
        elif source_text.startswith(("http://", "https://")):
            try:
                data = await download_image(
                    self.http,
                    source_text,
                    max_bytes=media.max_bytes,
                    timeout_s=cfg.int("interaction.media.download_timeout_s", 10, lo=1, hi=120),
                    max_redirects=cfg.int("interaction.media.max_redirects", 3, lo=0, hi=10),
                    validator=self.url_validator,
                )
            except MediaFetchError as exc:
                return {"ok": False, "message": str(exc)}
            kind, source_ref = "url", (urlparse(source_text).hostname or "")
        else:
            return {"ok": False, "message": "我只能存当前消息里的图，或者一个 http(s) 链接。"}

        ext = sniff_image_ext(data)
        if ext is None:
            return {"ok": False, "message": "这不是一张我能存的图。"}
        if len(data) > media.max_bytes:
            return {"ok": False, "message": f"这张图太大了（上限 {human_size(media.max_bytes)}）。"}

        max_files = cfg.int("interaction.media.max_files", 100, lo=1, hi=10000)
        max_total = cfg.int("interaction.media.max_total_mb", 200, lo=1, hi=100000) * 1024 * 1024
        stats = media.stats()
        if stats["files"] >= max_files:
            return {"ok": False, "message": f"文件夹满了（{max_files} 张），先删几张吧。"}
        if stats["bytes"] + len(data) > max_total:
            return {
                "ok": False,
                "message": f"文件夹满了（{human_size(max_total)}），先删几张吧。",
            }

        body = (description or "").strip()
        limit = cfg.int("interaction.media.max_description_chars", 200, lo=10, hi=2000)
        body = body[:limit]
        if not body and self.captioner is not None and cfg.bool("vision.enabled", True):
            caption: Any = None
            vision_limit = min(
                cfg.int("vision.max_chars", 80, lo=1, hi=500), limit
            )
            try:
                caption = await self.captioner(
                    data,
                    mime=IMAGE_MIME_HINTS.get(ext, "image/png"),
                    prompt=cfg.str("vision.prompt", DEFAULT_VISION_PROMPT),
                    bot_id=bot_id,
                    umo=source_umo,
                    model=cfg.str("vision.model", "deepseek-flash"),
                    max_chars=vision_limit,
                    timeout_s=cfg.int("vision.timeout_s", 20, lo=1, hi=120),
                    max_tokens=cfg.int("vision.max_tokens", 800, lo=100, hi=4000),
                    disable_thinking=cfg.bool("vision.disable_thinking", True),
                )
            except Exception as exc:  # noqa: BLE001 - 描述失败不阻断存图
                self.audit.warn("自动图片描述失败：%s", exc)
                caption = None
            if caption:
                body = str(caption).strip()[:vision_limit]
            else:
                await self._log(bot_id, "save", "", 0, source_text, "caption_failed", "")
        if not body:
            body = (name or "").strip() or (
                "对话里的一张图" if kind == "event" else "链接存下的图"
            )
        cleaned_tags = _clean_tags(tags)

        try:
            saved = media.save_bytes(data, prefix=name, now=self.now_fn())
        except MediaError as exc:
            return {"ok": False, "message": str(exc)}
        try:
            await self.db.execute(
                "INSERT INTO media_items(bot_id, filename, description, tags, source, source_ref, "
                "sender_id, sender_name, source_umo, size, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bot_id,
                    saved["filename"],
                    body,
                    json.dumps(cleaned_tags, ensure_ascii=False),
                    kind,
                    source_ref,
                    sender_id,
                    sender_name,
                    source_umo,
                    saved["size"],
                    self.now_fn(),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 元数据写失败时回滚文件
            try:
                media.delete(saved["filename"])
            except MediaError:
                pass
            self.audit.warn("写图片元数据失败：%s", exc)
            return {"ok": False, "message": "这张图没存下来。"}
        await self._log(bot_id, "save", saved["filename"], saved["size"], source_text, "ok", "")
        self.audit.event(
            "media_save", bot_id=bot_id, filename=saved["filename"], size=saved["size"], source=kind
        )
        return {
            "ok": True,
            "filename": saved["filename"],
            "size": saved["size"],
            "message": f"存好了：{saved['filename']}（{human_size(saved['size'])}）",
        }

    # ------------------------------------------------------------------ 查询
    async def _rows(self, bot_id: str, limit: int = 300) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            "SELECT * FROM media_items WHERE bot_id=? ORDER BY created_at DESC LIMIT ?",
            (bot_id, max(1, int(limit))),
        )

    def _existing(self, media: MediaDir, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for row in rows:
            try:
                media.path_of(str(row.get("filename") or ""))
            except MediaError:
                continue
            kept.append(row)
        return kept

    async def list(self, bot_id: str, limit: int = 10) -> list[dict[str, Any]]:
        media = self.media(bot_id)
        rows = self._existing(media, await self._rows(bot_id, 300))
        return [_row_out(row) for row in rows[: max(1, int(limit))]]

    async def search(self, bot_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        media = self.media(bot_id)
        rows = self._existing(media, await self._rows(bot_id, 300))
        tokens = bigrams(query)
        now = self.now_fn()
        if not tokens:
            return [_row_out(row) for row in rows[: max(1, int(limit))]]
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            tags = " ".join(_clean_tags(_row_out(row)["tags"]))
            haystack = " ".join(
                [
                    str(row.get("description") or ""),
                    tags,
                    str(row.get("filename") or ""),
                    str(row.get("sender_name") or ""),
                ]
            )
            score = overlap(tokens, haystack)
            if score <= 0:
                continue
            age_days = max(0.0, (now - float(row.get("created_at") or now)) / DAY)
            recency = max(0.0, 1.0 - age_days / 180.0)
            scored.append((score + 0.15 * recency, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [_row_out(row) for _, row in scored[: max(1, int(limit))]]

    # ------------------------------------------------------------------ 删除
    async def delete(self, bot_id: str, filename: str) -> dict[str, Any]:
        media = self.media(bot_id)
        try:
            media.delete(filename)
        except MediaError as exc:
            return {"ok": False, "message": str(exc)}
        await self.db.execute(
            "DELETE FROM media_items WHERE bot_id=? AND filename=?", (bot_id, filename)
        )
        await self._log(bot_id, "delete", filename, None, "manual", "ok", "")
        self.audit.event("media_delete", bot_id=bot_id, filename=filename)
        return {"ok": True, "message": f"删掉了：{filename}"}

    async def stats(self, bot_id: str) -> dict[str, int]:
        disk = self.media(bot_id).stats()
        described = int(
            await self.db.scalar(
                "SELECT COUNT(*) FROM media_items WHERE bot_id=? AND description<>''",
                (bot_id,),
                default=0,
            )
            or 0
        )
        return {"files": disk["files"], "bytes": disk["bytes"], "described": described}

    # ------------------------------------------------------------------ 发图
    async def resolve_for_send(
        self, event: Any, *, bot_id: str, source: str
    ) -> dict[str, Any]:
        media = self.media(bot_id)
        cfg = self.configs(bot_id)
        text = str(source or "").strip()
        filename: str | None = None
        if text.startswith("event:"):
            try:
                index = int(text.split(":", 1)[1])
            except (ValueError, IndexError) as exc:
                raise MediaError("event 引用要写成 event:1 这样。") from exc
            data = await media.read_event_image(event, index)
        elif text.startswith(("http://", "https://")):
            try:
                data = await download_image(
                    self.http,
                    text,
                    max_bytes=media.max_bytes,
                    timeout_s=cfg.int("interaction.media.download_timeout_s", 10, lo=1, hi=120),
                    max_redirects=cfg.int("interaction.media.max_redirects", 3, lo=0, hi=10),
                    validator=self.url_validator,
                )
            except MediaFetchError as exc:
                raise MediaError(str(exc)) from exc
        elif text:
            data = await media.read(text)
            filename = text
        else:
            raise MediaError("要发哪张图？可以写 event:1、图片文件名，或一个 http(s) 链接。")
        if sniff_image_ext(data) is None:
            raise MediaError("这不是一张我能发的图。")
        if len(data) > media.max_bytes:
            raise MediaError(f"这张图太大了（上限 {human_size(media.max_bytes)}）。")
        return {"data": data, "filename": filename}

    async def mark_used(self, bot_id: str, filename: str, now: int | None = None) -> None:
        await self.db.execute(
            "UPDATE media_items SET last_used_at=?, use_count=use_count+1 "
            "WHERE bot_id=? AND filename=?",
            (now or self.now_fn(), bot_id, filename),
        )

    async def log_send(self, bot_id: str, filename: str | None, status: str, error: str = "") -> None:
        await self._log(bot_id, "send", filename or "", None, "chat", status, error)

    # ------------------------------------------------------------------ 审计
    async def _log(
        self,
        bot_id: str,
        action: str,
        filename: str,
        size: int | None,
        source: str,
        status: str,
        error: str,
    ) -> None:
        await self.db.execute(
            "INSERT INTO media_logs(bot_id, action, filename, size, source, status, error, "
            "created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                bot_id,
                action,
                filename[:200],
                size,
                str(source)[:200],
                status,
                (error or "")[:300],
                self.now_fn(),
            ),
        )
