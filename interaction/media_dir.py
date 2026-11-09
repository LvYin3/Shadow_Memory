"""图片库目录：按机器人隔离、格式嗅探、原子写入、白名单读取与删除。"""

from __future__ import annotations

import base64
import hashlib
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
IMAGE_MIME_HINTS = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


class MediaError(ValueError):
    """图片库的拒绝原因，文案直接面向模型。"""


def safe_bot_id(bot_id: str | None) -> str:
    cleaned = "".join(
        char if (char.isalnum() or char in "._-") else "_" for char in (bot_id or "")
    )
    return (cleaned[:64] or "default")


def sniff_image_ext(data: bytes | None) -> str | None:
    """按文件头判断真实图片格式；不认识就返回 None。"""
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def sanitize_prefix(prefix: str | None, max_chars: int = 32) -> str:
    raw = (prefix or "").strip()
    kept: list[str] = []
    for char in raw:
        if char.isalnum() or char in "_-":
            kept.append(char)
        elif char.isspace():
            kept.append("-")
    cleaned = "".join(kept).strip("-_")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned[: max(1, int(max_chars))]


def human_size(size: int) -> str:
    return f"{size / 1024.0:.1f} KB" if size < 1024 * 1024 else f"{size / 1048576.0:.1f} MB"


class MediaDir:
    def __init__(
        self,
        root: Path,
        bot_id: str,
        *,
        max_images: int = 9,
        max_image_mb: float = 5.0,
        allow_event_images: bool = True,
        max_name_chars: int = 32,
    ) -> None:
        self.root = Path(root).resolve()
        self.bot_id = bot_id or ""
        self.media_dir = (self.root / safe_bot_id(bot_id)).resolve()
        self.max_images = max(1, int(max_images))
        self.max_bytes = int(max(0.1, float(max_image_mb)) * 1024 * 1024)
        self.allow_event_images = bool(allow_event_images)
        self.max_name_chars = max(1, int(max_name_chars))

    # ------------------------------------------------------------------ 目录
    def ensure_dir(self) -> Path:
        self.media_dir.mkdir(parents=True, exist_ok=True)
        return self.media_dir

    def list_local(self) -> list[dict[str, Any]]:
        if not self.media_dir.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(
            self.media_dir.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True
        ):
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTS:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append(
                {
                    "name": path.name,
                    "size_kb": round(stat.st_size / 1024.0, 1),
                    "updated": int(stat.st_mtime * 1000),
                }
            )
        return items

    def stats(self) -> dict[str, int]:
        files = 0
        total = 0
        if self.media_dir.is_dir():
            for path in self.media_dir.iterdir():
                if not path.is_file() or path.suffix.lower() not in IMAGE_EXTS:
                    continue
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
                files += 1
        return {"files": files, "bytes": total}

    # ------------------------------------------------------------------ 路径
    def path_of(self, filename: str) -> Path:
        name = (filename or "").strip()
        if not name or any(sep in name for sep in ("/", "\\", "..")):
            raise MediaError(f"图片只能写文件名：{name or '（空）'}")
        path = (self.media_dir / name).resolve()
        if path.parent != self.media_dir:
            raise MediaError(f"图片不在允许的目录里：{name}")
        if path.suffix.lower() not in IMAGE_EXTS:
            raise MediaError(f"不支持的图片格式：{name}")
        if not path.is_file():
            raise MediaError(f"图片不存在：{name}")
        return path

    async def read(self, filename: str) -> bytes:
        path = self.path_of(filename)
        data = path.read_bytes()
        if len(data) > self.max_bytes:
            raise MediaError(f"图片太大：{filename}")
        return data

    def delete(self, filename: str) -> None:
        path = self.path_of(filename)
        try:
            path.unlink()
        except OSError as exc:
            raise MediaError(f"删不掉这张图：{exc}") from exc

    # ------------------------------------------------------------------ 写入
    def save_bytes(
        self, data: bytes, *, prefix: str = "", now: int | None = None
    ) -> dict[str, Any]:
        ext = sniff_image_ext(data)
        if ext is None:
            raise MediaError("这不是一张我能存的图。")
        if len(data) > self.max_bytes:
            raise MediaError(f"这张图太大了（上限 {human_size(self.max_bytes)}）。")
        stamp = datetime.fromtimestamp((now or 0) / 1000.0).strftime("%Y%m%d-%H%M%S")
        digest = hashlib.sha1(data).hexdigest()[:6]
        cleaned = sanitize_prefix(prefix, self.max_name_chars)
        base = f"{cleaned}-{stamp}-{digest}" if cleaned else f"{stamp}-{digest}"
        temp: Path | None = None
        try:
            self.ensure_dir()
            filename = f"{base}.{ext}"
            counter = 2
            while (self.media_dir / filename).exists():
                filename = f"{base}-{counter}.{ext}"
                counter += 1
            target = self.media_dir / filename
            temp = self.media_dir / f".{filename}.tmp-{uuid.uuid4().hex[:8]}"
            temp.write_bytes(data)
            os.replace(temp, target)
        except OSError as exc:
            try:
                if temp is not None and temp.exists():
                    temp.unlink()
            except OSError:
                pass
            raise MediaError(f"写不进图片目录：{exc}") from exc
        return {"filename": filename, "size": len(data), "ext": ext}

    # ------------------------------------------------------------------ 来源解析
    async def read_event_image(self, event: Any, index: int) -> bytes:
        if not self.allow_event_images:
            raise MediaError("当前配置不允许引用消息里的图片。")
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        images = [
            component
            for component in message
            if getattr(component, "type", "") == "image"
            or component.__class__.__name__ == "Image"
        ]
        if index < 1 or index > len(images):
            raise MediaError(f"这条消息里没有第 {index} 张图。")
        component = images[index - 1]
        converter = getattr(component, "convert_to_base64", None)
        if converter is None:
            raise MediaError("这张图取不出来。")
        encoded = await converter()
        if not encoded:
            raise MediaError("这张图取不出来。")
        text = str(encoded)
        if text.startswith("data:") and "," in text:
            text = text.split(",", 1)[1]
        try:
            data = base64.b64decode(text, validate=False)
        except (ValueError, TypeError) as exc:
            raise MediaError("这张图取不出来。") from exc
        if not data:
            raise MediaError("这张图取不出来。")
        if len(data) > self.max_bytes:
            raise MediaError(f"这张图太大了（上限 {human_size(self.max_bytes)}）。")
        return data

    async def resolve(self, items: Iterable[Any] | None, event: Any = None) -> list[str]:
        """把发说说用的图片引用解析成 file:// / http(s) / base64://。"""
        refs: list[str] = []
        values = list(items or [])
        if len(values) > self.max_images:
            raise MediaError(f"最多 {self.max_images} 张图片")
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            if text.startswith(("http://", "https://")):
                refs.append(text)
                continue
            if text.startswith("event:"):
                try:
                    index = int(text.split(":", 1)[1])
                except (ValueError, IndexError) as exc:
                    raise MediaError(f"event 图片引用不合法：{text}") from exc
                data = await self.read_event_image(event, index)
                refs.append(f"base64://{base64.b64encode(data).decode('ascii')}")
                continue
            path = self.path_of(text)
            if path.stat().st_size > self.max_bytes:
                raise MediaError(f"图片太大：{text}")
            refs.append(path.as_uri())
        return refs
