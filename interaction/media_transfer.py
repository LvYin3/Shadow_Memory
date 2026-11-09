"""跨机器人复制媒体文件与 media_items 行，覆盖时先移入 trash。"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path
from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.db import Database
from .media import MediaService
from .media_dir import safe_bot_id

MAX_FILES = 500
MAX_BYTES = 100 * 1024 * 1024


class MediaTransferError(ValueError):
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


class MediaTransfer:
    def __init__(
        self,
        db: Database,
        media: MediaService,
        data_dir: Path,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.media = media
        self.data_dir = Path(data_dir)
        self.now_fn = now_fn

    async def _rows(self, bot_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM media_items WHERE bot_id=? ORDER BY id", (bot_id,)
        )
        return [dict(row) for row in rows]

    async def preview(self, source: str, target: str) -> dict[str, Any]:
        rows = await self._rows(source)
        source_dir = self.media.media(source)
        target_dir = self.media.media(target)

        def _scan() -> dict[str, Any]:
            files = bytes_total = missing = 0
            for row in rows:
                try:
                    path = source_dir.path_of(str(row.get("filename") or ""))
                except Exception:  # noqa: BLE001
                    missing += 1
                    continue
                if not path.is_file():
                    missing += 1
                    continue
                files += 1
                bytes_total += path.stat().st_size
            existing = {
                str(item["name"])
                for item in target_dir.list_local()
                if item.get("name")
            }
            return {
                "items": len(rows),
                "files": files,
                "bytes": bytes_total,
                "missing": missing,
                "target_files": len(existing),
            }

        return await asyncio.to_thread(_scan)

    def _trash_dir(self, bot_id: str) -> Path:
        stamp = self.now_fn()
        folder = self.data_dir / "exports" / f"trash_{safe_bot_id(bot_id)}_{stamp}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    async def prepare(
        self, source: str, target: str, *, replace: bool
    ) -> dict[str, Any]:
        rows = await self._rows(source)
        source_dir = self.media.media(source)
        target_dir = self.media.media(target)
        target_dir.ensure_dir()

        def _do() -> dict[str, Any]:
            copied: list[Path] = []
            plan_rows: list[dict[str, Any]] = []
            moved: list[tuple[Path, Path]] = []
            failed: list[str] = []
            skipped = renamed = 0
            trash: Path | None = None
            used = {
                str(item["name"])
                for item in target_dir.list_local()
                if item.get("name")
            }
            if replace:
                trash = self._trash_dir(target)
                for item in target_dir.list_local():
                    name = str(item.get("name") or "")
                    if not name:
                        continue
                    try:
                        src = target_dir.path_of(name)
                    except Exception:  # noqa: BLE001
                        continue
                    if not src.is_file():
                        continue
                    dest = trash / name
                    shutil.move(str(src), str(dest))
                    moved.append((src, dest))
                used = set()
            for row in rows:
                name = str(row.get("filename") or "")
                try:
                    src_path = source_dir.path_of(name)
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{name}:{exc}")
                    continue
                if not src_path.is_file():
                    failed.append(name)
                    continue
                try:
                    digest = _sha256(src_path)
                except OSError:
                    failed.append(name)
                    continue
                final_name = name
                if final_name in used:
                    existing = target_dir.path_of(final_name)
                    if existing.is_file():
                        try:
                            same = _sha256(existing) == digest
                        except OSError:
                            same = False
                        if same:
                            skipped += 1
                            continue
                    final_name = _unique_name(final_name, used)
                    renamed += 1
                try:
                    dest_path = (target_dir.media_dir / final_name).resolve()
                    if dest_path.parent != target_dir.media_dir:
                        raise ValueError("invalid media name")
                    shutil.copy2(src_path, dest_path)
                except Exception:  # noqa: BLE001
                    failed.append(name)
                    continue
                copied.append(dest_path)
                used.add(final_name)
                plan_row = dict(row)
                plan_row["filename"] = final_name
                plan_row["size"] = dest_path.stat().st_size
                plan_rows.append(plan_row)
            total = sum(int(item.get("size") or 0) for item in plan_rows)
            if len(plan_rows) > MAX_FILES or total > MAX_BYTES:
                for path in copied:
                    path.unlink(missing_ok=True)
                for src, dest in moved:
                    shutil.move(str(dest), str(src))
                raise MediaTransferError(
                    f"媒体超过转移上限（{MAX_FILES} 文件 / {MAX_BYTES // 1048576} MB）"
                )
            return {
                "rows": plan_rows,
                "copied": copied,
                "moved": moved,
                "trash": trash,
                "failed": failed,
                "skipped": skipped,
                "renamed": renamed,
                "bytes": total,
                "replace": replace,
            }

        return await asyncio.to_thread(_do)

    async def rollback(self, plan: dict[str, Any]) -> None:
        def _do() -> None:
            for path in plan.get("copied") or []:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
            for src, dest in plan.get("moved") or []:
                try:
                    Path(src).parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dest), str(src))
                except OSError:
                    pass

        await asyncio.to_thread(_do)
