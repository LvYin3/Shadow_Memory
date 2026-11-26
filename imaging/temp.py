"""拍照成图临时暂存区（photo:<id>），按 bot_id 隔离并自动过期清理。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.config import ConfigView
from ..core.db import Database
from ..interaction.media_dir import MediaDir, MediaError

ConfigProvider = Callable[[str], ConfigView]
HOUR_MS = 3_600_000


class PhotoTempStore:
    def __init__(
        self,
        db: Database,
        configs: ConfigProvider,
        data_dir: Path,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.data_dir = Path(data_dir)
        self.now_fn = now_fn

    def temp_dir(self, bot_id: str) -> MediaDir:
        max_mb = self.configs(bot_id).float("image.max_image_mb", 8.0, lo=1.0, hi=30.0)
        return MediaDir(self.data_dir / "photo_temp", bot_id, max_image_mb=max_mb)

    async def save(
        self,
        bot_id: str,
        data: bytes,
        *,
        prompt: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        now = self.now_fn()
        ttl_hours = self.configs(bot_id).int("image.temp_ttl_hours", 24, lo=1, hi=168)
        expires_at = now + ttl_hours * HOUR_MS
        photo_id = uuid.uuid4().hex[:10]
        mdir = self.temp_dir(bot_id)
        saved = mdir.save_bytes(data, prefix=f"p-{photo_id}", now=now)
        try:
            await self.db.execute(
                "INSERT INTO image_temp(id, bot_id, filename, prompt, description, size, "
                "created_at, expires_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    photo_id,
                    bot_id,
                    saved["filename"],
                    (prompt or "")[:2000] or None,
                    (description or "")[:300],
                    saved["size"],
                    now,
                    expires_at,
                ),
            )
        except Exception:
            try:
                mdir.delete(saved["filename"])
            except MediaError:
                pass
            raise
        return {
            "id": photo_id,
            "handle": f"photo:{photo_id}",
            "filename": saved["filename"],
            "size": saved["size"],
            "expires_at": expires_at,
        }

    async def get_meta(self, bot_id: str, photo_id: str) -> dict[str, Any] | None:
        pid = (photo_id or "").strip()
        if not pid:
            return None
        row = await self.db.fetchone(
            "SELECT * FROM image_temp WHERE id=? AND bot_id=?", (pid, bot_id)
        )
        if row is None:
            return None
        if int(row.get("expires_at") or 0) <= self.now_fn():
            return None
        return dict(row)

    async def read(self, bot_id: str, photo_id: str) -> bytes:
        pid = (photo_id or "").strip()
        if not pid:
            raise MediaError("照片引用为空。")
        row = await self.db.fetchone(
            "SELECT * FROM image_temp WHERE id=? AND bot_id=?", (pid, bot_id)
        )
        if row is None:
            raise MediaError(f"找不到这张刚拍的照片：photo:{pid}")
        if int(row.get("expires_at") or 0) <= self.now_fn():
            raise MediaError(f"这张临时照片已经过期了：photo:{pid}")
        return await self.temp_dir(bot_id).read(str(row["filename"]))

    async def cleanup(self, now: int | None = None) -> int:
        stamp = now or self.now_fn()
        expired = await self.db.fetchall(
            "SELECT id, bot_id, filename FROM image_temp WHERE expires_at<=?", (stamp,)
        )
        removed = 0
        for row in expired:
            await self.db.execute("DELETE FROM image_temp WHERE id=?", (row["id"],))
            try:
                self.temp_dir(str(row["bot_id"])).delete(str(row["filename"]))
            except MediaError:
                pass
            removed += 1
        return removed
