"""WebUI 概览/明细/设置/导入导出编排（不依赖 AstrBot 框架）。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView, gate_question_text, resolve_gate_question
from ..core.db import Database
from ..core.kv import KvStore
from ..imaging.providers import PROTOCOL_LABELS, PROTOCOLS, normalize_provider
from ..imaging.refs import REF_CATEGORIES, REF_CATEGORY_LABELS, RefError
from ..interaction.media import MediaService
from ..memory.ambient import AmbientService
from ..memory.seed import SeedError
from ..memory.service import MemoryService
from ..memory.transfer import TransferService
from ..story import tree as story_tree
from ..story.world import CATEGORY_LABELS, WorldSeedError

BACKUP_KEEP = 10
GATE_QUESTION_MAX_CHARS = 2000


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
        ambient: AmbientService,
        media: MediaService,
        transfer: TransferService,
        kv: KvStore,
        data_dir: Path,
        audit: Audit,
        tts_providers: Callable[[], list[dict[str, str]]],
        platform_ids: Callable[[], list[str]] | None = None,
        now_fn: Callable[[], int] = clock_mod.now_ms,
        story: Any = None,
        image: Any = None,
        config_raw: Any = None,
        save_config: Callable[[], None] | None = None,
    ) -> None:
        self.story = story
        self.image = image
        self._config_raw = config_raw
        self._save_config = save_config
        self.db = db
        self.configs = configs
        self.memory = memory
        self.ambient = ambient
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
        rows = await self.db.fetchall("SELECT bot_id FROM story_state")
        ids.update(str(row["bot_id"]) for row in rows if row.get("bot_id"))
        rows = await self.db.fetchall("SELECT DISTINCT bot_id FROM image_refs")
        ids.update(str(row["bot_id"]) for row in rows if row.get("bot_id"))
        npc_ids = await self.db.fetchall("SELECT npc_bot_id FROM story_npcs")
        ids.difference_update(str(row["npc_bot_id"]) for row in npc_ids)
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

    async def rebuild_event(self, bot_id: str, event_id: int) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        if int(event_id) <= 0:
            raise WebUIError("事件 ID 不合法")
        try:
            return await self.memory.rebuild_event(bot_id, int(event_id))
        except ValueError as exc:
            raise WebUIError(str(exc)) from exc

    async def detail(self, bot_id: str) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        ambient = await self.ambient.snapshot(bot_id)
        return {
            "bot_id": bot_id,
            "ambient": {
                "text": ambient.text,
                "recent": ambient.recent,
                "people": ambient.people,
                "important": ambient.important,
            },
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
                "SELECT e.id, e.fragment, e.summary, e.salience, e.status, e.ended_at, e.source, "
                "EXISTS(SELECT 1 FROM event_turns t WHERE t.event_id=e.id) AS has_turns "
                "FROM events e WHERE e.bot_id=? AND e.merged_into IS NULL "
                "ORDER BY e.ended_at DESC LIMIT 20",
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
            gate_question, gate_source = resolve_gate_question(
                self.configs(bot_id), await self.kv.get(bot_id, "gate_question")
            )
            bots.append(
                {
                    "bot_id": bot_id,
                    "tts_override": await self.kv.get(bot_id, "tts_provider_id"),
                    "tts_provider_id": effective,
                    "tts_source": source,
                    "context_recent_messages": await self.kv.get(
                        bot_id, "context_recent_messages"
                    ),
                    "gate_question": gate_question,
                    "gate_question_source": gate_source,
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
        gate_question: str | None = None,
        clear_tts: bool = False,
        clear_context: bool = False,
    ) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        # 先校验再落库：门控问题不合法时，同一次保存里的其它项也不写。
        question: str | None = None
        follow_config = False
        if gate_question is not None:
            if not isinstance(gate_question, str):
                raise WebUIError("门控问题必须是文本")
            question = gate_question_text(gate_question)
            # 文本框预填的是当前生效的问题：空或与配置层一致都表示“跟随配置”，不留覆盖。
            configured, _ = resolve_gate_question(self.configs(bot_id))
            follow_config = not question or question == configured
            # 配置层自己的问题不受长度限制，原样提交回来不能报错。
            if not follow_config and len(question) > GATE_QUESTION_MAX_CHARS:
                raise WebUIError(f"门控问题不能超过 {GATE_QUESTION_MAX_CHARS} 字")
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
        if question is not None:
            if follow_config:
                await self.kv.delete(bot_id, "gate_question")
            else:
                await self.kv.set(bot_id, "gate_question", question)
        return {
            "bot_id": bot_id,
            "tts_provider_id": await self.kv.get(bot_id, "tts_provider_id"),
            "context_recent_messages": await self.kv.get(
                bot_id, "context_recent_messages"
            ),
            "gate_question": await self.kv.get(bot_id, "gate_question"),
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
        elif kind == "story":
            # 完整的故事数据：世界种子、设定集、主持人笔记、场景转录。含剧透。
            payload = await self._story().export(bot_id)
        else:
            raise WebUIError("kind 只能是 full / seed / story")
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
        if version.startswith("world_seed"):
            try:
                result = await self._story().import_seed(bot_id, payload)
            except ValueError as exc:
                raise WebUIError(str(exc)) from exc
            return {"bot_id": bot_id, "kind": "world_seed", "counts": result, "backup": ""}
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

    # ------------------------------------------------------------------ 故事
    def _story(self) -> Any:
        if self.story is None:
            raise WebUIError("故事模块还没准备好")
        return self.story

    async def story_overview(self) -> dict[str, Any]:
        """故事页签：运维信息和她的视角。不含完整转录、设定集正文和主持人笔记。"""
        story = self._story()
        return {
            "master_enabled": story.master_enabled(),
            "bots": [await story.status(bot_id) for bot_id in await self.bot_ids()],
        }

    async def story_action(self, bot_id: str, action: str) -> dict[str, Any]:
        """开关类操作直接执行；``wake`` 只做检查，真正的演出由调用方放到后台。"""
        bot_id = self._require_bot_id(bot_id)
        story = self._story()
        handlers = {
            "on": story.enable,
            "off": story.disable,
            "pause": story.pause,
            "resume": story.resume,
            "refill": story.refill_day,
        }
        try:
            if action == "wake":
                await story.check_wake(bot_id)
                return {"bot_id": bot_id, "started": True}
            handler = handlers.get(action)
            if handler is None:
                raise WebUIError("action 只能是 on / off / pause / resume / wake / refill")
            return await handler(bot_id)
        except WebUIError:
            raise
        except Exception as exc:  # noqa: BLE001 - StoryError 等统一转成页面可读的错误
            raise WebUIError(str(exc)) from exc

    async def story_spoiler(self, bot_id: str) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        story = self._story()
        heavy = self.configs(bot_id).bool("story.spoiler_heavy", False)
        scenes = await self.db.fetchall(
            "SELECT id, started_at, status, transcript FROM story_scenes "
            "WHERE bot_id=? ORDER BY id DESC LIMIT 30",
            (bot_id,),
        )
        payload: dict[str, Any] = {
            "bot_id": bot_id,
            "spoiler_heavy": heavy,
            "categories": [
                {"id": key, "label": label} for key, label in CATEGORY_LABELS.items()
            ],
            "scenes": [
                {
                    "id": row["id"],
                    "started_at": row["started_at"],
                    "status": row["status"],
                    "transcript": _json_list(row.get("transcript")),
                }
                for row in scenes
            ],
            "canon": await story.store.canon(bot_id),
            "seed": await story.store.seed_form(bot_id),
            "tree": story_tree.loads((await story.store.state(bot_id) or {}).get("tree")),
        }
        if not heavy:
            return payload
        payload["notes"] = await story.store.notes(bot_id)
        payload["npcs"] = await self._npc_memories(bot_id)
        return payload

    async def story_save_seed(self, bot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        try:
            return await self._story().import_seed(bot_id, payload)
        except (WorldSeedError, ValueError) as exc:
            raise WebUIError(str(exc)) from exc

    async def story_classify(self, bot_id: str) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        try:
            return await self._story().classify_world(bot_id)
        except Exception as exc:  # noqa: BLE001
            raise WebUIError(str(exc)) from exc

    async def story_set_canon_visibility(
        self, bot_id: str, canon_id: int, visibility: str
    ) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        try:
            return await self._story().set_canon_visibility(bot_id, int(canon_id), visibility)
        except Exception as exc:  # noqa: BLE001
            raise WebUIError(str(exc)) from exc

    async def story_save_note(self, bot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        self._require_heavy(bot_id)
        title = str(payload.get("title") or "").strip()[:40]
        body = str(payload.get("body") or "").strip()[:600]
        if not title or not body:
            raise WebUIError("笔记要有标题和正文")
        note_id = int(payload.get("id") or 0)
        now = self.now_fn()
        try:
            if note_id:
                ok = await self._story().store.update_note(bot_id, note_id, title, body, now)
                if not ok:
                    raise WebUIError("没有这条笔记")
                return {"id": note_id}
            new_id = await self.db.execute(
                "INSERT INTO story_notes(bot_id, title, body, scene_id, created_at, updated_at) "
                "VALUES(?,?,?,NULL,?,?)",
                (bot_id, title, body, now, now),
            )
        except WebUIError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WebUIError("这则笔记没能保存，标题可能和已有的重复") from exc
        return {"id": new_id}

    async def story_delete_note(self, bot_id: str, note_id: int) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        self._require_heavy(bot_id)
        ok = await self._story().store.delete_note(bot_id, int(note_id))
        if not ok:
            raise WebUIError("没有这条笔记")
        return {"id": int(note_id), "deleted": True}

    def _require_heavy(self, bot_id: str) -> None:
        if not self.configs(bot_id).bool("story.spoiler_heavy", False):
            raise WebUIError("重度剧透没打开")

    async def _npc_memories(self, owner: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for row in await self._story().store.npcs(owner):
            npc_id = str(row["npc_bot_id"])
            persona = await self.db.fetchall(
                "SELECT layer, text FROM persona_entries WHERE bot_id=? AND active=1 ORDER BY id",
                (npc_id,),
            )
            facts = await self.db.fetchall(
                "SELECT text FROM facts WHERE bot_id=? AND archived=0 ORDER BY id DESC LIMIT 20",
                (npc_id,),
            )
            events = await self.db.fetchall(
                "SELECT fragment FROM events WHERE bot_id=? AND fragment IS NOT NULL "
                "ORDER BY id DESC LIMIT 5",
                (npc_id,),
            )
            found.append(
                {
                    "name": row["name"],
                    "status": row["status"],
                    "last_seen_at": row.get("last_seen_at"),
                    "relationship": row.get("relationship") or "",
                    "persona": [{"layer": item["layer"], "text": item["text"]} for item in persona],
                    "facts": [str(item["text"]) for item in facts if item.get("text")],
                    "fragments": [str(item["fragment"]) for item in events if item.get("fragment")],
                }
            )
        return found

    # ------------------------------------------------------------------ 生图
    def _image(self) -> Any:
        if self.image is None:
            raise WebUIError("生图模块还没准备好")
        return self.image

    def _raw_providers(self) -> list[dict[str, Any]]:
        if isinstance(self._config_raw, dict):
            sec = self._config_raw.get("image")
            if isinstance(sec, dict) and isinstance(sec.get("providers"), list):
                return [
                    normalize_provider(item)
                    for item in sec["providers"]
                    if isinstance(item, dict)
                ]
        cfg_list = self.configs("").get("image.providers", [])
        if isinstance(cfg_list, list):
            return [
                normalize_provider(item)
                for item in cfg_list
                if isinstance(item, dict)
            ]
        return []

    def _masked_providers(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in self._raw_providers():
            masked = dict(item)
            masked["api_key_set"] = bool(masked.pop("api_key", ""))
            out.append(masked)
        return out

    async def image_overview(self, bot_id: str = "") -> dict[str, Any]:
        img = self._image()
        bots = await self.bot_ids()
        chosen = (bot_id or "").strip()
        if chosen:
            chosen = self._require_bot_id(chosen)
        elif bots:
            chosen = bots[0]

        global_cfg = self.configs("")
        active_refs: list[dict[str, Any]] = []
        pending: dict[str, Any] = {
            "total": 0,
            "cap_total": global_cfg.int("image.ref.max_pending_total", 15, lo=1, hi=200),
            "cap_per_subject": global_cfg.int("image.ref.max_pending_per_subject", 3, lo=1, hi=20),
            "full": False,
            "groups": [],
        }
        usage = {"used": 0, "limit": global_cfg.int("image.daily_limit", 20, lo=0, hi=1000)}
        recent_logs: list[dict[str, Any]] = []
        bot_enabled = global_cfg.bool("image.enabled", False)
        bot_provider_id = global_cfg.str("image.provider_id", "")
        story_enabled = global_cfg.bool("story.enabled", False)

        if chosen:
            cfg = self.configs(chosen)
            bot_enabled = cfg.bool("image.enabled", False)
            bot_provider_id = cfg.str("image.provider_id", "")
            story_enabled = cfg.bool("story.enabled", False)
            active_refs = await img.refs.list_active(chosen)
            pending = await img.refs.pending_summary(chosen)
            used, limit = await img.daily_usage(chosen)
            usage = {"used": used, "limit": limit}
            recent_logs = await self.db.fetchall(
                "SELECT id, kind, provider_id, protocol, ref_count, retries, review_status, "
                "status, error, latency_ms, created_at FROM image_logs "
                "WHERE bot_id=? ORDER BY id DESC LIMIT 15",
                (chosen,),
            )

        return {
            "bots": bots,
            "bot": chosen,
            "enabled": global_cfg.bool("image.enabled", False),
            "bot_enabled": bot_enabled,
            "story_enabled": story_enabled,
            "default_provider_id": global_cfg.str("image.provider_id", ""),
            "bot_provider_id": bot_provider_id,
            "providers": self._masked_providers(),
            "protocols": [
                {"id": p, "label": PROTOCOL_LABELS.get(p, p)} for p in PROTOCOLS
            ],
            "categories": [
                {"id": c, "label": REF_CATEGORY_LABELS.get(c, c)}
                for c in REF_CATEGORIES
            ],
            "usage": usage,
            "active_refs": active_refs,
            "pending": pending,
            "recent_logs": recent_logs,
        }

    async def image_ref_file(self, bot_id: str, ref_id: int) -> tuple[Path, str, str]:
        bot_id = self._require_bot_id(bot_id)
        img = self._image()
        try:
            row, _, mime = await img.refs.read_bytes(bot_id, int(ref_id))
        except RefError as exc:
            raise WebUIError(str(exc)) from exc
        mdir = (
            img.refs.pending_dir(bot_id)
            if row["status"] == "pending"
            else img.refs.active_dir(bot_id)
        )
        return mdir.path_of(row["filename"]), row["filename"], mime

    async def image_ref_data_url(self, bot_id: str, ref_id: int) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        img = self._image()
        try:
            row, data, mime = await img.refs.read_bytes(bot_id, int(ref_id))
        except RefError as exc:
            raise WebUIError(str(exc)) from exc
        encoded = base64.b64encode(data).decode("ascii")
        return {
            "id": row["id"],
            "mime": mime,
            "data_url": f"data:{mime};base64,{encoded}",
        }

    async def image_ref_upload(self, bot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        img = self._image()
        raw_b64 = str(payload.get("data_base64") or "").strip()
        if raw_b64.startswith("data:") and "," in raw_b64:
            raw_b64 = raw_b64.split(",", 1)[1]
        if not raw_b64:
            raise WebUIError("缺少图片数据")
        try:
            data = base64.b64decode(raw_b64, validate=False)
        except (ValueError, TypeError) as exc:
            raise WebUIError("图片 base64 解码失败") from exc
        try:
            item = await img.refs.add_active(
                bot_id,
                category=str(payload.get("category") or ""),
                name=str(payload.get("name") or ""),
                data=data,
                description=str(payload.get("description") or ""),
                source="upload",
            )
        except RefError as exc:
            raise WebUIError(str(exc)) from exc
        return {"ok": True, "item": item}

    async def image_ref_update(self, bot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        img = self._image()
        ref_id = int(payload.get("id") or 0)
        if ref_id <= 0:
            raise WebUIError("缺少参考图 ID")
        try:
            item = await img.refs.update_ref(
                bot_id,
                ref_id,
                name=payload.get("name") if "name" in payload else None,
                description=payload.get("description") if "description" in payload else None,
                category=payload.get("category") if "category" in payload else None,
            )
        except RefError as exc:
            raise WebUIError(str(exc)) from exc
        return {"ok": True, "item": item}

    async def image_ref_delete(self, bot_id: str, ref_id: int) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        img = self._image()
        try:
            await img.refs.delete_ref(bot_id, int(ref_id))
        except RefError as exc:
            raise WebUIError(str(exc)) from exc
        return {"ok": True, "id": int(ref_id)}

    async def image_pending_approve(
        self, bot_id: str, ref_id: int, *, description: Any = None
    ) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        img = self._image()
        try:
            item = await img.refs.approve_pending(
                bot_id,
                int(ref_id),
                description=str(description) if description is not None else None,
            )
        except RefError as exc:
            raise WebUIError(str(exc)) from exc
        return {"ok": True, "item": item}

    async def image_pending_reject(self, bot_id: str, ref_id: int) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        img = self._image()
        try:
            await img.refs.reject_pending(bot_id, int(ref_id))
        except RefError as exc:
            raise WebUIError(str(exc)) from exc
        return {"ok": True, "id": int(ref_id)}

    async def image_pending_regenerate(
        self, bot_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        img = self._image()
        result = await img.generate_auto_ref(
            bot_id=bot_id,
            category=str(payload.get("category") or ""),
            name=str(payload.get("name") or ""),
            appearance=str(payload.get("description") or payload.get("appearance") or ""),
            force=True,
        )
        if not result.get("ok"):
            raise WebUIError(str(result.get("message") or "补生参考图失败"))
        return result

    async def image_providers_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(self._config_raw, dict):
            raise WebUIError("当前配置对象不支持写入")
        raw_list = payload.get("providers")
        if not isinstance(raw_list, list):
            raise WebUIError("providers 必须是数组")

        existing_keys = {
            item["id"]: item.get("api_key", "")
            for item in self._raw_providers()
            if item.get("id")
        }
        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for idx, entry in enumerate(raw_list):
            if not isinstance(entry, dict):
                raise WebUIError(f"第 {idx + 1} 条提供商不是对象")
            norm = normalize_provider(entry)
            if not norm["id"]:
                raise WebUIError(f"第 {idx + 1} 条提供商缺少 ID")
            if norm["id"] in seen:
                raise WebUIError(f"提供商 ID 重复：{norm['id']}")
            seen.add(norm["id"])
            if not norm["base_url"] or not norm["model"]:
                raise WebUIError(f"提供商 {norm['id']} 需要填写 base_url 和 model")
            if not norm["name"]:
                norm["name"] = norm["id"]
            if entry.get("clear_api_key"):
                norm["api_key"] = ""
            elif not norm["api_key"]:
                norm["api_key"] = existing_keys.get(norm["id"], "")
            cleaned.append(norm)

        image_sec = self._config_raw.get("image")
        if not isinstance(image_sec, dict):
            image_sec = {}
            self._config_raw["image"] = image_sec
        image_sec["providers"] = cleaned
        if "default_provider_id" in payload:
            default_id = str(payload.get("default_provider_id") or "").strip()
            if default_id and default_id not in seen:
                raise WebUIError(f"默认提供商 ID 不存在：{default_id}")
            image_sec["provider_id"] = default_id
        if "enabled" in payload:
            image_sec["enabled"] = bool(payload.get("enabled"))

        if self._save_config is not None:
            try:
                self._save_config()
            except Exception as exc:  # noqa: BLE001
                raise WebUIError(f"配置已生效但写回文件失败：{exc}") from exc
        return {
            "ok": True,
            "providers": self._masked_providers(),
            "default_provider_id": str(image_sec.get("provider_id") or ""),
            "enabled": bool(image_sec.get("enabled", False)),
        }

    async def image_providers_test(self, payload: dict[str, Any]) -> dict[str, Any]:
        img = self._image()
        raw = payload.get("provider") if isinstance(payload.get("provider"), dict) else payload
        norm = normalize_provider(raw)
        if not norm["api_key"] and norm["id"]:
            for item in self._raw_providers():
                if item["id"] == norm["id"]:
                    norm["api_key"] = item.get("api_key", "")
                    break
        return await img.test_provider(norm)

    async def image_test_photo(
        self, bot_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        bot_id = self._require_bot_id(bot_id)
        img = self._image()
        scene = str(payload.get("scene") or "").strip()
        if not scene:
            raise WebUIError("请输入原始画面描述")
        include_story: bool | None = None
        if "include_story" in payload and payload.get("include_story") is not None:
            include_story = bool(payload.get("include_story"))
        result = await img.test_photo(
            bot_id=bot_id,
            scene=scene,
            provider_id=str(payload.get("provider_id") or "").strip(),
            include_story=include_story,
            recent_chat=str(payload.get("recent_chat") or ""),
            run_review=bool(payload.get("run_review", False)),
        )
        if not result.get("ok"):
            raise WebUIError(str(result.get("error") or "测试生图失败"))
        data: bytes = result["data"]
        mime = str(result.get("mime") or "image/png")
        ext = str(result.get("ext") or "png")
        encoded = base64.b64encode(data).decode("ascii")
        return {
            "ok": True,
            "bot_id": bot_id,
            "data_url": f"data:{mime};base64,{encoded}",
            "mime": mime,
            "ext": ext,
            "filename": f"{_safe(bot_id)}-test-{self.now_fn()}.{ext}",
            "size": int(result.get("size") or len(data)),
            "provider_id": result.get("provider_id") or "",
            "provider_name": result.get("provider_name") or "",
            "protocol": result.get("protocol") or "",
            "prompt": result.get("prompt") or "",
            "aspect_ratio": result.get("aspect_ratio") or "",
            "refs": result.get("refs") or [],
            "context_text": result.get("context_text") or "",
            "review": result.get("review"),
            "translate_meta": result.get("translate_meta") or {},
            "latency_ms": int(result.get("latency_ms") or 0),
        }

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


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
