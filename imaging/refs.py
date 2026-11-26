"""参考图库与人工审核区存储（按 bot_id 隔离）。

五类参考图：
- style（画风）：允许保留多张生效图，每次生图固定全部附带；
- self（角色本人）：允许保留多张生效图（如常服/不同角度）；
- person / place / item：每个事物保留 1 张正式生效图；
待审区（status='pending'）：同一事物最多 3 张候选，全库默认上限 15 张，
人工选定 1 张生效后自动清理该事物在审核区里的其余候选。
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.config import ConfigView
from ..core.db import Database
from ..interaction.media_dir import (
    IMAGE_MIME_HINTS,
    MediaDir,
    MediaError,
    human_size,
    sniff_image_ext,
)
from .providers import RefImageInput

ConfigProvider = Callable[[str], ConfigView]

REF_CATEGORIES = ("style", "self", "person", "place", "item")
REF_CATEGORY_LABELS = {
    "style": "画风",
    "self": "角色本人",
    "person": "人物",
    "place": "场景",
    "item": "物件",
}
_CATEGORY_ALIASES = {label: key for key, label in REF_CATEGORY_LABELS.items()}
_CATEGORY_ALIASES.update(
    {
        "主角": "self",
        "本人": "self",
        "自己": "self",
        "风格": "style",
        "角色": "person",
        "人": "person",
        "地点": "place",
        "场所": "place",
        "物品": "item",
        "东西": "item",
    }
)


class RefError(ValueError):
    """参考图操作被拒绝（文案直接展示给 WebUI 或日志）。"""


def normalize_ref_category(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in REF_CATEGORIES:
        return raw
    mapped = _CATEGORY_ALIASES.get(str(value or "").strip())
    if mapped:
        return mapped
    raise RefError(f"不支持的参考图分类：{value or '（空）'}")


def _row_out(row: dict[str, Any]) -> dict[str, Any]:
    size = int(row.get("size") or 0)
    category = str(row.get("category") or "item")
    return {
        "id": int(row["id"]),
        "bot_id": str(row.get("bot_id") or ""),
        "category": category,
        "category_label": REF_CATEGORY_LABELS.get(category, category),
        "name": str(row.get("name") or ""),
        "description": str(row.get("description") or ""),
        "filename": str(row.get("filename") or ""),
        "size": size,
        "size_kb": round(size / 1024.0, 1),
        "source": str(row.get("source") or "upload"),
        "status": str(row.get("status") or "active"),
        "prompt": str(row.get("prompt") or ""),
        "scene_id": row.get("scene_id"),
        "created_at": int(row.get("created_at") or 0),
        "updated_at": int(row.get("updated_at") or 0),
    }


class RefStore:
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

    def active_dir(self, bot_id: str) -> MediaDir:
        max_mb = self.configs(bot_id).float("image.max_image_mb", 8.0, lo=1.0, hi=30.0)
        return MediaDir(self.data_dir / "image_refs", bot_id, max_image_mb=max_mb)

    def pending_dir(self, bot_id: str) -> MediaDir:
        max_mb = self.configs(bot_id).float("image.max_image_mb", 8.0, lo=1.0, hi=30.0)
        return MediaDir(self.data_dir / "image_pending", bot_id, max_image_mb=max_mb)

    def _dir_for_status(self, bot_id: str, status: str) -> MediaDir:
        return self.pending_dir(bot_id) if status == "pending" else self.active_dir(bot_id)

    def _validate_meta(self, category: Any, name: Any, description: Any) -> tuple[str, str, str]:
        cat = normalize_ref_category(category)
        clean_name = str(name or "").strip()[:40]
        if not clean_name:
            if cat == "style":
                clean_name = "画风参考"
            elif cat == "self":
                clean_name = "角色本人"
            else:
                raise RefError("名称不能为空")
        clean_desc = str(description or "").strip()[:300]
        return cat, clean_name, clean_desc

    # ------------------------------------------------------------------ 查询
    async def list_active(
        self, bot_id: str, category: str | None = None
    ) -> list[dict[str, Any]]:
        if category:
            cat = normalize_ref_category(category)
            rows = await self.db.fetchall(
                "SELECT * FROM image_refs WHERE bot_id=? AND status='active' AND category=? "
                "ORDER BY id DESC",
                (bot_id, cat),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM image_refs WHERE bot_id=? AND status='active' "
                "ORDER BY CASE category WHEN 'style' THEN 0 WHEN 'self' THEN 1 "
                "WHEN 'person' THEN 2 WHEN 'place' THEN 3 ELSE 4 END, id DESC",
                (bot_id,),
            )
        mdir = self.active_dir(bot_id)
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                mdir.path_of(str(row.get("filename") or ""))
            except MediaError:
                continue
            out.append(_row_out(row))
        return out

    async def list_pending(self, bot_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM image_refs WHERE bot_id=? AND status='pending' "
            "ORDER BY category, name, created_at ASC, id ASC",
            (bot_id,),
        )
        mdir = self.pending_dir(bot_id)
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                mdir.path_of(str(row.get("filename") or ""))
            except MediaError:
                continue
            out.append(_row_out(row))
        return out

    async def pending_summary(self, bot_id: str) -> dict[str, Any]:
        cfg = self.configs(bot_id)
        cap_total = cfg.int("image.ref.max_pending_total", 15, lo=1, hi=200)
        cap_per = cfg.int("image.ref.max_pending_per_subject", 3, lo=1, hi=20)
        items = await self.list_pending(bot_id)
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for item in items:
            key = (item["category"], item["name"])
            group = grouped.get(key)
            if group is None:
                group = {
                    "category": item["category"],
                    "category_label": item["category_label"],
                    "name": item["name"],
                    "description": item["description"],
                    "items": [],
                }
                grouped[key] = group
            group["items"].append(item)
            if not group["description"] and item["description"]:
                group["description"] = item["description"]
        return {
            "total": len(items),
            "cap_total": cap_total,
            "cap_per_subject": cap_per,
            "full": len(items) >= cap_total,
            "groups": list(grouped.values()),
        }

    async def get(self, bot_id: str, ref_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM image_refs WHERE bot_id=? AND id=?", (bot_id, int(ref_id))
        )
        return _row_out(row) if row is not None else None

    async def read_bytes(self, bot_id: str, ref_id: int) -> tuple[dict[str, Any], bytes, str]:
        row = await self.db.fetchone(
            "SELECT * FROM image_refs WHERE bot_id=? AND id=?", (bot_id, int(ref_id))
        )
        if row is None:
            raise RefError("参考图不存在")
        mdir = self._dir_for_status(bot_id, str(row.get("status") or "active"))
        try:
            data = await mdir.read(str(row["filename"]))
        except MediaError as exc:
            raise RefError(str(exc)) from exc
        ext = sniff_image_ext(data) or "png"
        return _row_out(row), data, IMAGE_MIME_HINTS.get(ext, "image/png")

    async def has_active(self, bot_id: str, category: str, name: str) -> bool:
        cat = normalize_ref_category(category)
        row = await self.db.fetchone(
            "SELECT 1 FROM image_refs WHERE bot_id=? AND status='active' "
            "AND category=? AND name=? LIMIT 1",
            (bot_id, cat, str(name or "").strip()),
        )
        return row is not None

    async def can_auto_generate(
        self, bot_id: str, category: str, name: str
    ) -> tuple[bool, str]:
        """检查该事物是否可以自动生成新参考图进审核区（或直接生效）。"""
        cfg = self.configs(bot_id)
        cat = normalize_ref_category(category)
        clean_name = str(name or "").strip()
        if not clean_name:
            return False, "名称为空"
        if cat not in ("person", "place", "item"):
            return False, "仅人物/场景/物件支持自动生成参考图"
        if await self.has_active(bot_id, cat, clean_name):
            return False, "已有生效参考图"
        if not cfg.bool("image.ref.require_review", True):
            return True, ""
        cap_total = cfg.int("image.ref.max_pending_total", 15, lo=1, hi=200)
        cap_per = cfg.int("image.ref.max_pending_per_subject", 3, lo=1, hi=20)
        total = int(
            await self.db.scalar(
                "SELECT COUNT(*) FROM image_refs WHERE bot_id=? AND status='pending'",
                (bot_id,),
                default=0,
            )
            or 0
        )
        if total >= cap_total:
            return False, f"审核区已满（{total}/{cap_total}）"
        per = int(
            await self.db.scalar(
                "SELECT COUNT(*) FROM image_refs WHERE bot_id=? AND status='pending' "
                "AND category=? AND name=?",
                (bot_id, cat, clean_name),
                default=0,
            )
            or 0
        )
        if per >= cap_per:
            return False, f"「{clean_name}」在审核区已有 {per}/{cap_per} 张候选"
        return True, ""

    # ------------------------------------------------------------------ 写入与审核
    async def add_active(
        self,
        bot_id: str,
        *,
        category: str,
        name: str,
        data: bytes,
        description: str = "",
        source: str = "upload",
        prompt: str = "",
        scene_id: int | None = None,
    ) -> dict[str, Any]:
        cat, clean_name, clean_desc = self._validate_meta(category, name, description)
        cfg = self.configs(bot_id)
        mdir = self.active_dir(bot_id)
        if sniff_image_ext(data) is None:
            raise RefError("不是支持的图片格式（仅支持 PNG/JPG/GIF/WEBP）")
        if len(data) > mdir.max_bytes:
            raise RefError(f"图片超过上限（{human_size(mdir.max_bytes)}）")

        if cat == "style":
            cap = cfg.int("image.max_style_refs", 3, lo=1, hi=20)
            count = len(await self.list_active(bot_id, "style"))
            if count >= cap:
                raise RefError(f"画风参考图已达上限（{cap} 张），请先删除旧图")
        elif cat == "self":
            cap = cfg.int("image.max_self_refs", 3, lo=1, hi=20)
            count = len(await self.list_active(bot_id, "self"))
            if count >= cap:
                raise RefError(f"角色本人参考图已达上限（{cap} 张），请先删除旧图")

        try:
            saved = mdir.save_bytes(data, prefix=f"{cat}-{clean_name}", now=self.now_fn())
        except MediaError as exc:
            raise RefError(str(exc)) from exc

        now = self.now_fn()
        old_files: list[str] = []
        try:
            async with self.db.transaction():
                if cat in ("person", "place", "item"):
                    existing = await self.db.fetchall(
                        "SELECT id, filename FROM image_refs WHERE bot_id=? AND status='active' "
                        "AND category=? AND name=?",
                        (bot_id, cat, clean_name),
                    )
                    for row in existing:
                        old_files.append(str(row["filename"]))
                        await self.db.execute(
                            "DELETE FROM image_refs WHERE id=?", (int(row["id"]),)
                        )
                ref_id = await self.db.execute(
                    "INSERT INTO image_refs(bot_id, category, name, description, filename, size, "
                    "source, status, prompt, scene_id, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        bot_id,
                        cat,
                        clean_name,
                        clean_desc,
                        saved["filename"],
                        saved["size"],
                        "auto" if source == "auto" else "upload",
                        "active",
                        (prompt or "")[:2000] or None,
                        scene_id,
                        now,
                        now,
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            try:
                mdir.delete(saved["filename"])
            except MediaError:
                pass
            raise RefError(f"写入参考图失败：{exc}") from exc

        for old_name in old_files:
            try:
                mdir.delete(old_name)
            except MediaError:
                pass
        row = await self.get(bot_id, ref_id)
        assert row is not None
        return row

    async def add_pending(
        self,
        bot_id: str,
        *,
        category: str,
        name: str,
        data: bytes,
        description: str = "",
        prompt: str = "",
        scene_id: int | None = None,
    ) -> dict[str, Any]:
        cat, clean_name, clean_desc = self._validate_meta(category, name, description)
        cfg = self.configs(bot_id)
        cap_total = cfg.int("image.ref.max_pending_total", 15, lo=1, hi=200)
        cap_per = cfg.int("image.ref.max_pending_per_subject", 3, lo=1, hi=20)
        total = int(
            await self.db.scalar(
                "SELECT COUNT(*) FROM image_refs WHERE bot_id=? AND status='pending'",
                (bot_id,),
                default=0,
            )
            or 0
        )
        if total >= cap_total:
            raise RefError(f"审核区已满（{total}/{cap_total}），已暂停自动生成")
        per = int(
            await self.db.scalar(
                "SELECT COUNT(*) FROM image_refs WHERE bot_id=? AND status='pending' "
                "AND category=? AND name=?",
                (bot_id, cat, clean_name),
                default=0,
            )
            or 0
        )
        if per >= cap_per:
            raise RefError(f"「{clean_name}」在审核区已达上限（{per}/{cap_per}）")

        mdir = self.pending_dir(bot_id)
        if sniff_image_ext(data) is None:
            raise RefError("不是支持的图片格式")
        if len(data) > mdir.max_bytes:
            raise RefError(f"图片超过上限（{human_size(mdir.max_bytes)}）")
        try:
            saved = mdir.save_bytes(data, prefix=f"pending-{cat}-{clean_name}", now=self.now_fn())
        except MediaError as exc:
            raise RefError(str(exc)) from exc

        now = self.now_fn()
        try:
            ref_id = await self.db.execute(
                "INSERT INTO image_refs(bot_id, category, name, description, filename, size, "
                "source, status, prompt, scene_id, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bot_id,
                    cat,
                    clean_name,
                    clean_desc,
                    saved["filename"],
                    saved["size"],
                    "auto",
                    "pending",
                    (prompt or "")[:2000] or None,
                    scene_id,
                    now,
                    now,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            try:
                mdir.delete(saved["filename"])
            except MediaError:
                pass
            raise RefError(f"写入审核区失败：{exc}") from exc
        row = await self.get(bot_id, ref_id)
        assert row is not None
        return row

    async def approve_pending(
        self, bot_id: str, ref_id: int, *, description: str | None = None
    ) -> dict[str, Any]:
        """人工审核通过：移入正式目录，替换同名旧图，清理同组其余待审候选。"""
        row = await self.db.fetchone(
            "SELECT * FROM image_refs WHERE bot_id=? AND id=? AND status='pending'",
            (bot_id, int(ref_id)),
        )
        if row is None:
            raise RefError("待审核参考图不存在")
        cat = str(row["category"])
        name = str(row["name"])
        cfg = self.configs(bot_id)
        if cat == "style":
            cap = cfg.int("image.max_style_refs", 3, lo=1, hi=20)
            if len(await self.list_active(bot_id, "style")) >= cap:
                raise RefError(f"画风参考图已达上限（{cap} 张）")
        elif cat == "self":
            cap = cfg.int("image.max_self_refs", 3, lo=1, hi=20)
            if len(await self.list_active(bot_id, "self")) >= cap:
                raise RefError(f"角色本人参考图已达上限（{cap} 张）")

        pdir = self.pending_dir(bot_id)
        adir = self.active_dir(bot_id)
        try:
            src_path = pdir.path_of(str(row["filename"]))
        except MediaError as exc:
            raise RefError(str(exc)) from exc

        adir.ensure_dir()
        new_name = str(row["filename"])
        if new_name.startswith("pending-"):
            new_name = new_name[len("pending-") :]
        target = adir.media_dir / new_name
        if target.exists():
            stem = target.stem
            new_name = f"{stem}-{uuid.uuid4().hex[:6]}{target.suffix}"
            target = adir.media_dir / new_name
        tmp = adir.media_dir / f".{new_name}.tmp-{uuid.uuid4().hex[:8]}"
        shutil.copy2(src_path, tmp)
        os.replace(tmp, target)

        now = self.now_fn()
        new_desc = (
            str(description).strip()[:300]
            if description is not None
            else str(row.get("description") or "")
        )
        old_active_files: list[str] = []
        sibling_pending_files: list[str] = []
        try:
            async with self.db.transaction():
                if cat in ("person", "place", "item"):
                    existing = await self.db.fetchall(
                        "SELECT id, filename FROM image_refs WHERE bot_id=? AND status='active' "
                        "AND category=? AND name=?",
                        (bot_id, cat, name),
                    )
                    for old in existing:
                        old_active_files.append(str(old["filename"]))
                        await self.db.execute(
                            "DELETE FROM image_refs WHERE id=?", (int(old["id"]),)
                        )
                siblings = await self.db.fetchall(
                    "SELECT id, filename FROM image_refs WHERE bot_id=? AND status='pending' "
                    "AND category=? AND name=? AND id<>?",
                    (bot_id, cat, name, int(ref_id)),
                )
                for sib in siblings:
                    sibling_pending_files.append(str(sib["filename"]))
                    await self.db.execute(
                        "DELETE FROM image_refs WHERE id=?", (int(sib["id"]),)
                    )
                await self.db.execute(
                    "UPDATE image_refs SET status='active', filename=?, description=?, "
                    "updated_at=? WHERE id=?",
                    (new_name, new_desc, now, int(ref_id)),
                )
        except Exception as exc:  # noqa: BLE001
            try:
                adir.delete(new_name)
            except MediaError:
                pass
            raise RefError(f"审核通过落库失败：{exc}") from exc

        for fname in [str(row["filename"]), *sibling_pending_files]:
            try:
                pdir.delete(fname)
            except MediaError:
                pass
        for fname in old_active_files:
            try:
                adir.delete(fname)
            except MediaError:
                pass
        updated = await self.get(bot_id, int(ref_id))
        assert updated is not None
        return updated

    async def reject_pending(self, bot_id: str, ref_id: int) -> bool:
        row = await self.db.fetchone(
            "SELECT * FROM image_refs WHERE bot_id=? AND id=? AND status='pending'",
            (bot_id, int(ref_id)),
        )
        if row is None:
            raise RefError("待审核参考图不存在")
        await self.db.execute("DELETE FROM image_refs WHERE id=?", (int(ref_id),))
        try:
            self.pending_dir(bot_id).delete(str(row["filename"]))
        except MediaError:
            pass
        return True

    async def update_ref(
        self,
        bot_id: str,
        ref_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT * FROM image_refs WHERE bot_id=? AND id=?", (bot_id, int(ref_id))
        )
        if row is None:
            raise RefError("参考图不存在")
        new_cat = normalize_ref_category(category) if category is not None else str(row["category"])
        new_name = (
            str(name).strip()[:40] if name is not None else str(row["name"])
        )
        if not new_name:
            raise RefError("名称不能为空")
        new_desc = (
            str(description).strip()[:300]
            if description is not None
            else str(row.get("description") or "")
        )
        await self.db.execute(
            "UPDATE image_refs SET category=?, name=?, description=?, updated_at=? WHERE id=?",
            (new_cat, new_name, new_desc, self.now_fn(), int(ref_id)),
        )
        updated = await self.get(bot_id, int(ref_id))
        assert updated is not None
        return updated

    async def delete_ref(self, bot_id: str, ref_id: int) -> bool:
        row = await self.db.fetchone(
            "SELECT * FROM image_refs WHERE bot_id=? AND id=?", (bot_id, int(ref_id))
        )
        if row is None:
            raise RefError("参考图不存在")
        mdir = self._dir_for_status(bot_id, str(row.get("status") or "active"))
        await self.db.execute("DELETE FROM image_refs WHERE id=?", (int(ref_id),))
        try:
            mdir.delete(str(row["filename"]))
        except MediaError:
            pass
        return True

    # ------------------------------------------------------------------ 生图装配
    async def active_catalog(self, bot_id: str) -> list[dict[str, Any]]:
        """给提示词转译模型看的非画风生效参考图精简目录。"""
        items = await self.list_active(bot_id)
        return [
            {
                "id": item["id"],
                "category": item["category"],
                "category_label": item["category_label"],
                "name": item["name"],
                "description": item["description"],
            }
            for item in items
            if item["category"] != "style"
        ]

    async def load_style_refs(self, bot_id: str) -> list[RefImageInput]:
        cfg = self.configs(bot_id)
        cap = cfg.int("image.max_style_refs", 3, lo=1, hi=20)
        items = (await self.list_active(bot_id, "style"))[:cap]
        return await self._to_inputs(bot_id, items)

    async def load_self_refs(self, bot_id: str) -> list[RefImageInput]:
        cfg = self.configs(bot_id)
        cap = cfg.int("image.max_self_refs", 3, lo=1, hi=20)
        items = (await self.list_active(bot_id, "self"))[:cap]
        return await self._to_inputs(bot_id, items)

    async def load_for_generation(
        self, bot_id: str, selected_ids: list[Any] | None = None
    ) -> tuple[list[RefImageInput], list[RefImageInput]]:
        """返回 (固定画风参考图列表, 选中的主体参考图列表)，均受上限裁剪。"""
        cfg = self.configs(bot_id)
        style_refs = await self.load_style_refs(bot_id)
        max_subjects = cfg.int("image.max_subject_refs", 4, lo=0, hi=12)
        if max_subjects <= 0 or not selected_ids:
            return style_refs, []

        active = {item["id"]: item for item in await self.list_active(bot_id)}
        picked: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw in selected_ids:
            try:
                rid = int(raw)
            except (TypeError, ValueError):
                continue
            if rid in seen or rid not in active:
                continue
            item = active[rid]
            if item["category"] == "style":
                continue
            seen.add(rid)
            picked.append(item)
            if len(picked) >= max_subjects:
                break
        subject_refs = await self._to_inputs(bot_id, picked)
        return style_refs, subject_refs

    async def _to_inputs(
        self, bot_id: str, items: list[dict[str, Any]]
    ) -> list[RefImageInput]:
        mdir = self.active_dir(bot_id)
        result: list[RefImageInput] = []
        for item in items:
            try:
                data = await mdir.read(item["filename"])
            except MediaError:
                continue
            ext = sniff_image_ext(data) or "png"
            result.append(
                RefImageInput(
                    category=item["category"],
                    name=item["name"],
                    description=item["description"],
                    data=data,
                    mime=IMAGE_MIME_HINTS.get(ext, "image/png"),
                    filename=item["filename"],
                )
            )
        return result
