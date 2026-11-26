"""跨机器人复制正式生效参考图（image_refs active），覆盖时先移入 trash。"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path
from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.db import Database
from ..interaction.media_dir import safe_bot_id
from .refs import RefStore

MAX_FILES = 300
MAX_BYTES = 100 * 1024 * 1024


class RefTransferError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_name(name: str, used: set[str]) -> str:
    path = Path(name)
    stem, suffix = path.stem, path.suffix
    index = 2
    while True:
        candidate = f"{stem}_{index}{suffix}"
        if candidate not in used:
            return candidate
        index += 1


class RefTransfer:
    def __init__(
        self,
        db: Database,
        refs: RefStore,
        data_dir: Path,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.refs = refs
        self.data_dir = Path(data_dir)
        self.now_fn = now_fn

    async def _rows(self, bot_id: str) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            "SELECT * FROM image_refs WHERE bot_id=? AND status='active' ORDER BY id",
            (bot_id,),
        )

    async def preview(self, source: str, target: str) -> dict[str, Any]:
        rows = await self._rows(source)
        src_dir = self.refs.active_dir(source)
        dst_dir = self.refs.active_dir(target)

        def _scan() -> dict[str, Any]:
            files = bytes_total = missing = 0
            for row in rows:
                try:
                    path = src_dir.path_of(str(row.get("filename") or ""))
                except Exception:  # noqa: BLE001
                    missing += 1
                    continue
                if not path.is_file():
                    missing += 1
                    continue
                files += 1
                bytes_total += path.stat().st_size
            existing = {
                p.name for p in dst_dir.media_dir.iterdir() if p.is_file()
            } if dst_dir.media_dir.is_dir() else set()
            return {
                "rows": len(rows),
                "files": files,
                "bytes": bytes_total,
                "missing": missing,
                "target_files": len(existing),
            }

        return await asyncio.to_thread(_scan)

    async def apply(
        self,
        source: str,
        target: str,
        *,
        mode: str = "merge",
    ) -> dict[str, Any]:
        if source == target:
            raise RefTransferError("来源与目标机器人相同")
        if mode not in {"merge", "replace"}:
            raise RefTransferError(f"不支持的迁移模式：{mode}")

        rows = await self._rows(source)
        src_dir = self.refs.active_dir(source)
        dst_dir = self.refs.active_dir(target)
        now = self.now_fn()

        def _prepare() -> tuple[list[tuple[dict[str, Any], Path]], int]:
            items: list[tuple[dict[str, Any], Path]] = []
            total_bytes = 0
            for row in rows:
                try:
                    path = src_dir.path_of(str(row.get("filename") or ""))
                except Exception:  # noqa: BLE001
                    continue
                if not path.is_file():
                    continue
                total_bytes += path.stat().st_size
                items.append((row, path))
            if len(items) > MAX_FILES or total_bytes > MAX_BYTES:
                raise RefTransferError("参考图数量或总容量超过单次迁移上限")
            return items, total_bytes

        items, _ = await asyncio.to_thread(_prepare)

        target_rows = await self._rows(target)
        existing_subjects = {
            (str(r["category"]), str(r["name"])) for r in target_rows
        }

        def _copy_files() -> list[tuple[dict[str, Any], str, int]]:
            dst_dir.ensure_dir()
            if mode == "replace" and dst_dir.media_dir.is_dir():
                trash_root = (
                    self.data_dir
                    / "trash"
                    / f"image_refs-{safe_bot_id(target)}-{now}"
                )
                moved = False
                for p in list(dst_dir.media_dir.iterdir()):
                    if p.is_file():
                        trash_root.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(p), str(trash_root / p.name))
                        moved = True
                _ = moved

            used_names = {
                p.name for p in dst_dir.media_dir.iterdir() if p.is_file()
            }
            existing_hashes = {
                _sha256(dst_dir.media_dir / name) for name in used_names
            }
            copied: list[tuple[dict[str, Any], str, int]] = []
            for row, src_path in items:
                cat = str(row["category"])
                name = str(row["name"])
                if mode == "merge":
                    if cat in ("person", "place", "item") and (cat, name) in existing_subjects:
                        continue
                    if _sha256(src_path) in existing_hashes:
                        continue
                fname = src_path.name
                if fname in used_names:
                    fname = _unique_name(fname, used_names)
                shutil.copy2(src_path, dst_dir.media_dir / fname)
                used_names.add(fname)
                existing_hashes.add(_sha256(src_path))
                copied.append((row, fname, src_path.stat().st_size))
            return copied

        copied = await asyncio.to_thread(_copy_files)
        async with self.db.transaction():
            if mode == "replace":
                await self.db.execute(
                    "DELETE FROM image_refs WHERE bot_id=? AND status='active'",
                    (target,),
                )
            for row, fname, size in copied:
                await self.db.execute(
                    "INSERT INTO image_refs(bot_id, category, name, description, filename, size, "
                    "source, status, prompt, scene_id, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        target,
                        row["category"],
                        row["name"],
                        row.get("description") or "",
                        fname,
                        size,
                        row.get("source") or "upload",
                        "active",
                        row.get("prompt"),
                        None,
                        int(row.get("created_at") or now),
                        now,
                    ),
                )
        return {"copied": len(copied), "skipped": len(items) - len(copied)}
