"""WebUI 概览/明细/设置/导入导出编排（不依赖 AstrBot 框架）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from ..core.kv import KvStore
from ..interaction.media import MediaService
from ..memory.seed import SeedError
from ..memory.service import MemoryService
from ..memory.transfer import TransferService

BACKUP_KEEP = 10


class WebUIError(ValueError):
    pass


def _safe(bot_id: str) -> str:
    return "".join(char if char.isalnum() or char in "-_." else "_" for char in bot_id)


class WebUIService:
    def __init__(
        self,
        db: Database,
        configs: Callable[[str], ConfigView],
        memory: MemoryService,
        media: MediaService,
        transfer: TransferService,
        kv: KvStore,
        data_dir: Path,
        audit: Audit,
        tts_providers: Callable[[], list[dict[str, str]]],
        platform_ids: Callable[[], list[str]] | None = None,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.memory = memory
        self.media = media
        self.transfer = transfer
        self.kv = kv
        self.data_dir = Path(data_dir)
        self.audit = audit
        self._tts_providers = tts_providers
        self._platform_ids = platform_ids or (lambda: [])
        self.now_fn = now_fn

    # ------------------------------------------------------------------ 机器人
    async def bot_ids(self) -> list[str]:
        ids = set(await self.memory.bot_ids())
        try:
            ids.update(str(item) for item in self._platform_ids() if item)
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("枚举平台实例失败：%s", exc)
        for key in ("bot_overrides", "interaction.tts_provider_ids"):
            section = self.configs("").section(key)
            if isinstance(section, dict):
                ids.update(str(item) for item in section.keys() if item)
        rows = await self.db.fetchall("SELECT DISTINCT bot_id FROM kv")
        ids.update(str(row["bot_id"]) for row in rows if row.get("bot_id"))
        return sorted(ids)

    async def _last_event(self, bot_id: str) -> int | None:
        value = await self.db.scalar(
            "SELECT MAX(COALESCE(ended_at, started_at)) FROM events WHERE bot_id=?",
            (bot_id,),
            default=None,
        )
        return int(value) if value else None

    async def overview(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        totals = {
            "persona": 0,
            "facts": 0,
            "events": 0,
            "insights": 0,
            "notes": 0,
            "media": 0,
        }
        for bot_id in await self.bot_ids():
            summary = await self.memory.summary(bot_id)
            media = await self.media.stats(bot_id)
            counts = summary.get("persona", {}).get("counts", {}) or {}
            facts = summary.get("facts", {}) or {}
            event_rows = summary.get("events", []) or []
            events = {"open": 0, "closed": 0, "total": 0}
            for item in event_rows:
                number = int(item.get("n") or 0)
                events["total"] += number
                if str(item.get("status")) == "open":
                    events["open"] += number
                else:
                    events["closed"] += number
            insights_total = int(
                await self.db.scalar(
                    "SELECT COUNT(*) FROM insights WHERE bot_id=?", (bot_id,), default=0
                )
                or 0
            )
            notes_total = int(
                await self.db.scalar(
                    "SELECT COUNT(*) FROM notes WHERE bot_id=?", (bot_id,), default=0
                )
                or 0
            )
            persona_total = sum(int(counts.get(layer) or 0) for layer in ("core", "edge", "tension"))
            totals["persona"] += persona_total
            totals["facts"] += int(facts.get("active") or facts.get("total") or 0)
            totals["events"] += events["total"]
            totals["insights"] += insights_total
            totals["notes"] += notes_total
            totals["media"] += int(media.get("files") or 0)
            cfg = self.configs(bot_id)
            rows.append(
                {
                    "bot_id": bot_id,
                    "persona": {
                        "counts": {layer: int(counts.get(layer) or 0) for layer in ("core", "edge", "tension")},
                        "chars": int(summary.get("persona", {}).get("chars") or 0),
                        "max_chars": int(summary.get("persona", {}).get("limits", {}).get("max") or 0),
                    },
                    "facts": facts,
                    "events": events,
                    "edges": summary.get("edges") or {},
                    "insights": {"total": insights_total, "unused": int(summary.get("insights_unused") or 0)},
                    "notes": {"total": notes_total},
                    "media": media,
                    "last_event_at": await self._last_event(bot_id),
                    "config": {
                        "persona_mode": cfg.str("persona.mode", "append"),
                        "gate_enabled": cfg.bool("interaction.gate_enabled", True),
                        "decision": "provider" if cfg.str("decision.provider_id").strip() else "direct",
                    },
                    "settings": {
                        "tts_provider_id": await self.kv.get(bot_id, "tts_provider_id"),
                        "context_recent_messages": await self.kv.get(
                            bot_id, "context_recent_messages"
                        ),
                    },
                }
            )
        return {"bots": rows, "totals": totals}

    async def detail(self, bot_id: str) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        return {
            "bot_id": bot_id,
            "persona": await self.db.fetchall(
                "SELECT layer, text FROM persona_entries WHERE bot_id=? AND active=1 "
                "ORDER BY layer, id LIMIT 20",
                (bot_id,),
            ),
            "facts": await self.db.fetchall(
                "SELECT text, kind, key_fact, valid_from FROM facts WHERE bot_id=? "
                "AND valid_to IS NULL AND archived=0 ORDER BY key_fact DESC, id DESC LIMIT 20",
                (bot_id,),
            ),
            "events": await self.db.fetchall(
                "SELECT fragment, summary, salience, status, ended_at FROM events "
                "WHERE bot_id=? AND merged_into IS NULL ORDER BY ended_at DESC LIMIT 20",
                (bot_id,),
            ),
            "insights": await self.db.fetchall(
                "SELECT text, created_at FROM insights WHERE bot_id=? ORDER BY id DESC LIMIT 20",
                (bot_id,),
            ),
            "notes": await self.db.fetchall(
                "SELECT kind, title, body, updated_at FROM notes WHERE bot_id=? "
                "ORDER BY updated_at DESC LIMIT 20",
                (bot_id,),
            ),
            "media": await self.db.fetchall(
                "SELECT filename, description, size, use_count FROM media_items "
                "WHERE bot_id=? ORDER BY last_used_at DESC, id DESC LIMIT 20",
                (bot_id,),
            ),
        }

    # ------------------------------------------------------------------ 设置
    async def settings(self) -> dict[str, Any]:
        bots = []
        for bot_id in await self.bot_ids():
            effective = await self.kv.get(bot_id, "tts_provider_id")
            source = "db"
            if effective is None:
                effective = str(
                    self.configs(bot_id).section("interaction.tts_provider_ids").get(bot_id)
                    or ""
                )
                source = "config" if effective else "none"
            elif not str(effective).strip():
                effective = ""
                source = "muted"
            bots.append(
                {
                    "bot_id": bot_id,
                    "tts_override": await self.kv.get(bot_id, "tts_provider_id"),
                    "tts_provider_id": effective,
                    "tts_source": source,
                    "context_recent_messages": await self.kv.get(
                        bot_id, "context_recent_messages"
                    ),
                }
            )
        return {
            "bots": bots,
            "tts_providers": self._tts_providers(),
            "defaults": {
                "context_recent_messages": self.configs("").int(
                    "context.recent_messages", 20, lo=0, hi=50
                ),
                "context_max_chars": self.configs("").int(
                    "context.max_chars", 2000, lo=200, hi=20000
                ),
            },
        }

    async def save_settings(
        self,
        bot_id: str,
        *,
        tts_provider_id: str | None = None,
        context_recent_messages: int | None = None,
        clear_tts: bool = False,
        clear_context: bool = False,
    ) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        if clear_tts:
            await self.kv.delete(bot_id, "tts_provider_id")
        elif tts_provider_id is not None:
            value = str(tts_provider_id).strip()
            if value:
                allowed = {item["id"] for item in self._tts_providers()}
                if value not in allowed:
                    raise WebUIError("TTS provider 不存在")
                await self.kv.set(bot_id, "tts_provider_id", value)
            else:
                await self.kv.set(bot_id, "tts_provider_id", "")
        if clear_context:
            await self.kv.delete(bot_id, "context_recent_messages")
        elif context_recent_messages is not None:
            try:
                limit = int(context_recent_messages)
            except (TypeError, ValueError) as exc:
                raise WebUIError("上下文条数必须是 0-50 的整数") from exc
            if not 0 <= limit <= 50:
                raise WebUIError("上下文条数必须是 0-50 的整数")
            await self.kv.set(bot_id, "context_recent_messages", str(limit))
        return {
            "bot_id": bot_id,
            "tts_provider_id": await self.kv.get(bot_id, "tts_provider_id"),
            "context_recent_messages": await self.kv.get(
                bot_id, "context_recent_messages"
            ),
        }

    async def clear_context_override(self, bot_id: str) -> None:
        await self.kv.delete(self._require_bot_id(bot_id), "context_recent_messages")

    # ------------------------------------------------------------------ 转移
    async def transfer_preview(
        self, source: str, target: str, categories: Iterable[str], mode: str
    ) -> dict[str, Any]:
        return await self.transfer.preview(
            self._require_bot_id(source),
            self._require_bot_id(target),
            categories,
            mode,
        )

    async def transfer_memory(
        self,
        source: str,
        target: str,
        categories: Iterable[str],
        mode: str,
        *,
        confirm_target: str | None = None,
    ) -> dict[str, Any]:
        source = self._require_bot_id(source)
        target = self._require_bot_id(target)
        backup = self._backup_path(target)
        result = await self.transfer.execute(
            source,
            target,
            categories,
            mode,
            backup_path=backup,
            confirm_target=confirm_target,
        )
        await self._prune_backups()
        return result

    # ------------------------------------------------------------------ 导入导出
    async def export_memory(self, bot_id: str, kind: str) -> tuple[Path, str]:
        bot_id = self._require_bot_id(bot_id)
        if kind == "full":
            payload = await self.memory.export_full(bot_id)
        elif kind == "seed":
            payload = await self.memory.export_seed(bot_id)
        else:
            raise WebUIError("kind 只能是 full / seed")
        folder = self.data_dir / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"{_safe(bot_id)}_{kind}_{self.now_fn()}.json"
        path = folder / filename
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path, filename

    async def import_memory(
        self, bot_id: str, payload: Any, *, force: bool = False
    ) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        if not isinstance(payload, dict):
            raise WebUIError("导入内容必须是 JSON 对象")
        version = str(payload.get("version") or "")
        backup = ""
        if version.startswith("persona_seed"):
            if force:
                path = self._backup_path(bot_id)
                await self.db.backup(path)
                backup = str(path)
            counts = await self.memory.import_seed(bot_id, payload, force=force)
        elif version.startswith("shadow_memory/full/"):
            path = self._backup_path(bot_id) if force else None
            counts = await self.memory.restore_full(
                bot_id, payload, force=force, backup_path=path
            )
            backup = str(path) if path else ""
        else:
            raise WebUIError(f"不认识的备份版本：{version or '(空)'}")
        if backup:
            await self._prune_backups()
        return {"bot_id": bot_id, "counts": counts, "backup": backup}

    # ------------------------------------------------------------------ 文件
    def _require_bot_id(self, bot_id: Any) -> str:
        value = str(bot_id or "").strip()
        if not value or len(value) > 128:
            raise WebUIError("bot_id 不合法")
        if not all(char.isalnum() or char in "_.:@" for char in value):
            raise WebUIError("bot_id 含有不允许的字符")
        return value

    def _backup_path(self, bot_id: str) -> Path:
        folder = self.data_dir / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"backup_{_safe(bot_id)}_{self.now_fn()}.db"

    async def _prune_backups(self) -> None:
        folder = self.data_dir / "exports"
        if not folder.is_dir():
            return
        paths = sorted(
            folder.glob("backup_*.db"), key=lambda item: item.stat().st_mtime, reverse=True
        )
        for path in paths[BACKUP_KEEP:]:
            try:
                path.unlink()
            except OSError:
                pass
