"""影忆 ShadowMemory（AstrBot 人格记忆插件）入口。

记忆模块与交互模块在这里接到 AstrBot 的钩子上：
- ``event_message_type(ALL)``：观察消息、群聊门控；
- ``on_llm_request``：人格块进 system prompt，回忆进临时内容段，最近 N 条聊天补进上下文；
- ``register_web_api``：WebUI 的概览 / 明细 / 设置 / 转移 / 导入导出；
- ``on_llm_response``：记录缓存用量与回复文本；
- ``on_decorating_result``：丢弃最终文本并触发 post_turn；
- ``llm_tool``：say / speak / silent / notes / memo / media / send_image / qzone / memory_search。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, file_response, json_response, request
from astrbot.core.agent.message import TextPart

from .core import clock as clock_mod
from .core.audit import Audit, redact
from .core.config import ConfigView, pick_vision_provider_id
from .core.db import Database
from .core.decision import JevClient, JevRouter, ProviderJevClient
from .core.http import HttpxClient
from .core.kv import KvStore
from .core.textgen import TextClient
from .core.types import Turn
from .interaction.context_inject import (
    builtin_group_context_enabled,
    format_block,
    missing_for_contexts,
    resolve_limit,
    trim_to_chars,
    without_current,
)
from .interaction.environment import build_environment_prompt, say_example_messages
from .interaction.gate import Gate
from .interaction.commands import command_args
from .interaction.media import MediaService
from .interaction.media_dir import MediaDir, MediaError
from .interaction.media_transfer import MediaTransfer
from .interaction.memos import MemoService
from .interaction.notes import NoteService
from .interaction.qzone import QzoneService
from .interaction.result import should_drop_final_text
from .interaction.sender import Sender, state_of
from .interaction.session_buffer import SessionBuffer
from .interaction.sessions import SessionCatalog
from .interaction.tts_binding import resolve_tts_provider_id
from .memory.consolidate import Consolidator
from .memory.decay import Decayer
from .memory.distill import Distiller
from .memory.dynamics import Dynamics
from .memory.edges import EdgeRepo
from .memory.facts import FactService
from .memory.insights import InsightRepo
from .memory.persona import PersonaService
from .memory.recall import RecallService
from .memory.recall_format import MEMORY_OWNERSHIP_PROMPT, format_recall_block
from .memory.seed import SeedError, SeedService
from .memory.segmenter import Segmenter
from .memory.service import MemoryService
from .memory.transfer import TransferError, TransferService
from .webui.service import WebUIError, WebUIService

PENDING_KEY = "shadow_memory_pending"
LLM_TEXT_KEY = "shadow_memory_llm_text"
LOGGED_KEY = "shadow_memory_logged"
POST_KEY = "shadow_memory_post_started"
MEMO_KEY = "shadow_memory_memo_id"


class Main(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context, config if isinstance(config, dict) else {})
        self.astrbot_config = config if isinstance(config, dict) else {}
        plugin_name = getattr(self, "name", "") or "shadow_memory"
        self.data_dir = StarTools.get_data_dir(plugin_name)
        self.db: Database | None = None
        self.memory: MemoryService | None = None
        self.gate_svc: Gate | None = None
        self.sender: Sender | None = None
        self.notes: NoteService | None = None
        self.memos: MemoService | None = None
        self.qzone: QzoneService | None = None
        self.media_lib: MediaService | None = None
        self.catalog: SessionCatalog | None = None
        self.kv: KvStore | None = None
        self.session_buffer: SessionBuffer | None = None
        self.webui: WebUIService | None = None
        self.audit = Audit(
            logger,
            debug=ConfigView(self.astrbot_config, "").bool("logging.debug", False),
        )
        self._bg_tasks: list[asyncio.Task] = []
        self._tasks: set[asyncio.Task] = set()
        self._job_ids: list[str] = []
        self._qzone_active = True
        self._media_active = True
        self._send_image_active = True
        self._tool_use_warned: set[str] = set()
        # 在 __init__ 就注册，避免 initialize 失败/未重载时页面能打开但接口 404。
        self._register_web_apis()

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.data_dir / "memory.db")
        await self.db.connect()
        self.kv = KvStore(self.db)
        self.session_buffer = SessionBuffer(self.db)
        self._build_services()
        self._register_web_apis()
        await self._register_jobs()
        await self._reconcile_memos()
        self._bg_tasks.append(asyncio.create_task(self._worker_loop(), name="pm-worker"))
        self._bg_tasks.append(asyncio.create_task(self._sweeper_loop(), name="pm-sweeper"))
        await self._refresh_media_tools()
        self.audit.event("plugin_ready", data_dir=str(self.data_dir))

    async def terminate(self) -> None:
        for task in self._bg_tasks:
            task.cancel()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        for job_id in self._job_ids:
            try:
                await self.context.cron_manager.delete_job(job_id)
            except Exception:  # noqa: BLE001 - 卸载时任务可能已失效
                pass
        self._job_ids.clear()
        if self.db is not None:
            await self.db.close()
            self.db = None

    # ------------------------------------------------------------------ 装配
    def cfg(self, bot_id: str = "") -> ConfigView:
        return ConfigView(self.astrbot_config, bot_id)

    def _build_services(self) -> None:
        assert self.db is not None
        db = self.db
        configs = self.cfg
        audit = self.audit
        http = HttpxClient()
        direct = JevClient(configs(""), http, audit)
        provider_id = configs("").str("decision.provider_id").strip()
        if provider_id:
            primary = ProviderJevClient(
                provider_id,
                self._resolve_decision_provider,
                audit,
                self._build_decision_question,
                max_retries=configs("").int("decision.max_retries", 1, lo=0, hi=3),
            )
            jev = JevRouter(primary, direct if direct.available else None)
        else:
            jev = direct
        textgen = TextClient(
            configs(""),
            http,
            audit,
            provider_resolver=self._resolve_text_provider,
            vision_resolver=self._resolve_vision_provider,
        )
        dynamics = Dynamics(configs(""))
        edges = EdgeRepo(db)
        persona = PersonaService(db, configs, audit)
        insights = InsightRepo(db)
        facts = FactService(db, configs, dynamics, textgen, audit)
        segmenter = Segmenter(db, configs, dynamics, edges, textgen, audit)
        recall = RecallService(db, configs, dynamics, edges, jev, persona, audit)
        distiller = Distiller(db, configs, persona, insights, textgen, audit)
        consolidator = Consolidator(
            db, configs, dynamics, edges, persona, insights, facts, textgen, audit
        )
        decayer = Decayer(db, configs, dynamics, facts, audit)
        seed = SeedService(db, configs, persona, facts, edges, dynamics, audit)
        self.jev = jev
        self.textgen = textgen
        self.persona = persona
        self.facts = facts
        self.memory = MemoryService(
            db=db,
            configs=configs,
            dynamics=dynamics,
            edges=edges,
            persona=persona,
            insights=insights,
            facts=facts,
            segmenter=segmenter,
            recall=recall,
            distiller=distiller,
            consolidator=consolidator,
            decayer=decayer,
            seed=seed,
            jev=jev,
            textgen=textgen,
            audit=audit,
        )
        self.catalog = SessionCatalog(db, configs)
        self.gate_svc = Gate(db, configs, jev, persona, audit)
        self.sender = Sender(
            configs,
            audit,
            tts_resolver=self._resolve_tts,
            send_to=self._send_to,
            record_factory=lambda path: Record(file=path),
            image_factory=lambda data: Image.fromBytes(data),
            text_factory=lambda text: Plain(text),
        )
        self.notes = NoteService(db)
        self.memos = MemoService(
            db, audit, scheduler=self._schedule_memo, deleter=self._delete_job
        )
        self.qzone = QzoneService(db, configs, self._media_dir, audit)
        self.media_lib = MediaService(
            db, configs, self._media_dir, http, audit, captioner=textgen.describe_image
        )
        media_transfer = MediaTransfer(db, self.media_lib, self.data_dir)
        transfer = TransferService(db, configs, persona, edges, media_transfer, audit)
        self.webui = WebUIService(
            db,
            configs,
            self.memory,
            self.media_lib,
            transfer,
            self.kv,
            self.data_dir,
            audit,
            tts_providers=self._list_tts_providers,
            platform_ids=self._list_platform_ids,
        )

    # ------------------------------------------------------------------ WebUI
    def _list_platform_ids(self) -> list[str]:
        try:
            instances = self.context.platform_manager.get_insts()
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("枚举平台实例失败：%s", exc)
            return []
        ids: list[str] = []
        for instance in instances:
            try:
                meta = instance.meta()
                platform_id = str(getattr(meta, "id", "") or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if platform_id:
                ids.append(platform_id)
        return sorted(set(ids))

    def _list_tts_providers(self) -> list[dict[str, str]]:
        try:
            providers = self.context.get_all_tts_providers()
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("列出 TTS provider 失败：%s", exc)
            return []
        items: list[dict[str, str]] = []
        for provider in providers:
            try:
                meta = provider.meta()
                provider_id = str(getattr(meta, "id", "") or "")
                model = str(getattr(meta, "model", "") or "")
            except Exception:  # noqa: BLE001
                provider_id = str(
                    (getattr(provider, "provider_config", {}) or {}).get("id", "") or ""
                )
                model = ""
            if not provider_id:
                continue
            items.append(
                {
                    "id": provider_id,
                    "label": f"{provider_id} ({model})" if model else provider_id,
                    "model": model,
                }
            )
        return sorted(items, key=lambda item: item["id"])

    def _register_web_apis(self) -> None:
        # Dashboard 用 metadata.name（shadow_memory）作为扩展路径前缀；
        # 目录名/历史别名兼容不同加载方式。
        names = {
            str(getattr(self, "name", "") or "").strip() or "shadow_memory",
            "shadow_memory",
            "astrbot_plugin_shadow_memory",
        }
        routes = [
            ("overview", self.web_overview, ["GET"], "memory overview"),
            ("detail", self.web_detail, ["GET"], "memory detail"),
            ("settings", self.web_settings_get, ["GET"], "memory settings"),
            ("settings", self.web_settings_save, ["POST"], "save settings"),
            ("event/rebuild", self.web_event_rebuild, ["POST"], "rebuild event"),
            ("transfer/preview", self.web_transfer_preview, ["POST"], "preview transfer"),
            ("transfer", self.web_transfer, ["POST"], "execute transfer"),
            ("export", self.web_export, ["GET"], "export memory"),
            ("import", self.web_import, ["POST"], "import memory"),
        ]
        registered: list[str] = []
        for name in sorted(names):
            for suffix, handler, methods, desc in routes:
                try:
                    self.context.register_web_api(
                        f"/{name}/{suffix}", handler, methods, desc
                    )
                    registered.append(f"/{name}/{suffix}")
                except Exception as exc:  # noqa: BLE001
                    self.audit.warn("注册 WebUI 路由失败 %s/%s：%s", name, suffix, exc)
        self.audit.event("webui_routes", routes=registered)

    @staticmethod
    def _web_user() -> str | None:
        try:
            return getattr(request, "username", None)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _web_error(exc: Exception):
        return error_response(str(exc), 400)

    async def web_overview(self):
        if not self._web_user():
            return error_response("未登录", 401)
        if self.webui is None:
            return error_response("插件还没准备好", 503)
        try:
            return json_response(await self.webui.overview())
        except Exception as exc:  # noqa: BLE001
            return self._web_error(exc)

    async def web_detail(self):
        if not self._web_user():
            return error_response("未登录", 401)
        if self.webui is None:
            return error_response("插件还没准备好", 503)
        try:
            return json_response(
                await self.webui.detail(str(request.query.get("bot", "") or ""))
            )
        except Exception as exc:  # noqa: BLE001
            return self._web_error(exc)

    async def web_settings_get(self):
        if not self._web_user():
            return error_response("未登录", 401)
        if self.webui is None:
            return error_response("插件还没准备好", 503)
        try:
            return json_response(await self.webui.settings())
        except Exception as exc:  # noqa: BLE001
            return self._web_error(exc)

    async def web_event_rebuild(self):
        if not self._web_user():
            return error_response("未登录", 401)
        if self.webui is None:
            return error_response("插件还没准备好", 503)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", 400)
        try:
            event_id = int(payload.get("event_id") or 0)
            result = await self.webui.rebuild_event(
                str(payload.get("bot") or ""), event_id
            )
            return json_response(result)
        except Exception as exc:  # noqa: BLE001
            return self._web_error(exc)

    async def web_settings_save(self):
        if not self._web_user():
            return error_response("未登录", 401)
        if self.webui is None:
            return error_response("插件还没准备好", 503)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", 400)
        try:
            result = await self.webui.save_settings(
                str(payload.get("bot") or ""),
                tts_provider_id=payload.get("tts_provider_id"),
                context_recent_messages=payload.get("context_recent_messages"),
                clear_tts=bool(payload.get("clear_tts")),
                clear_context=bool(payload.get("clear_context")),
            )
            return json_response(result)
        except Exception as exc:  # noqa: BLE001
            return self._web_error(exc)

    async def web_transfer_preview(self):
        if not self._web_user():
            return error_response("未登录", 401)
        if self.webui is None:
            return error_response("插件还没准备好", 503)
        payload = await request.json(default={})
        try:
            result = await self.webui.transfer_preview(
                str(payload.get("source") or ""),
                str(payload.get("target") or ""),
                payload.get("categories") or [],
                str(payload.get("mode") or "merge"),
            )
            return json_response(result)
        except Exception as exc:  # noqa: BLE001
            return self._web_error(exc)

    async def web_transfer(self):
        if not self._web_user():
            return error_response("未登录", 401)
        if self.webui is None:
            return error_response("插件还没准备好", 503)
        payload = await request.json(default={})
        try:
            result = await self.webui.transfer_memory(
                str(payload.get("source") or ""),
                str(payload.get("target") or ""),
                payload.get("categories") or [],
                str(payload.get("mode") or "merge"),
                confirm_target=payload.get("confirm_target"),
            )
            return json_response(result)
        except Exception as exc:  # noqa: BLE001
            return self._web_error(exc)

    async def web_export(self):
        if not self._web_user():
            return error_response("未登录", 401)
        if self.webui is None:
            return error_response("插件还没准备好", 503)
        try:
            path, filename = await self.webui.export_memory(
                str(request.query.get("bot", "") or ""),
                str(request.query.get("kind", "full") or "full"),
            )
            return file_response(
                path, filename=filename, content_type="application/json"
            )
        except Exception as exc:  # noqa: BLE001
            return self._web_error(exc)

    async def web_import(self):
        if not self._web_user():
            return error_response("未登录", 401)
        if self.webui is None:
            return error_response("插件还没准备好", 503)
        raw = await request.body()
        if len(raw) > 10 * 1024 * 1024:
            return error_response("备份文件超过 10MB", 413)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return error_response("不是合法的 JSON 文件", 400)
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象", 400)
        try:
            result = await self.webui.import_memory(
                str(payload.get("bot") or ""),
                payload.get("payload"),
                force=bool(payload.get("force")),
            )
            return json_response(result)
        except Exception as exc:  # noqa: BLE001
            return self._web_error(exc)

    # ------------------------------------------------------------------ 作业
    async def _register_jobs(self) -> None:
        cfg = self.cfg("")
        jobs = [
            ("persona-memory-decay", "0 3 * * *", self._job_decay),
            ("persona-memory-facts", "15 3 * * *", self._job_facts),
            ("persona-memory-consolidate", cfg.str("consolidation.cron", "30 3 * * *"), self._job_consolidate),
            ("persona-memory-cleanup", "0 4 * * *", self._job_cleanup),
        ]
        for name, cron, handler in jobs:
            try:
                job = await self.context.cron_manager.add_basic_job(
                    name=name,
                    cron_expression=cron,
                    handler=handler,
                    persistent=False,
                )
                self._job_ids.append(job.job_id)
            except Exception as exc:  # noqa: BLE001 - 单个作业失败不阻断启动
                self.audit.warn("注册作业 %s 失败：%s", name, exc)

    async def _job_decay(self) -> None:
        if self.memory is None:
            return
        for bot_id in await self.memory.bot_ids():
            try:
                await self.memory.decay(bot_id)
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("衰减失败 %s：%s", bot_id, exc)

    async def _job_facts(self) -> None:
        if self.memory is None:
            return
        for bot_id in await self.memory.bot_ids():
            try:
                await self.memory.extract_facts(bot_id)
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("事实抽取失败 %s：%s", bot_id, exc)

    async def _job_consolidate(self) -> None:
        if self.memory is None:
            return
        for bot_id in await self.memory.bot_ids():
            try:
                await self.memory.consolidate(bot_id)
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("巩固失败 %s：%s", bot_id, exc)

    async def _job_cleanup(self) -> None:
        if self.db is None:
            return
        days = self.cfg("").int("logging.retention_days", 90, lo=1, hi=3650)
        cutoff = clock_mod.now_ms() - days * 86_400_000
        for table in (
            "gate_logs",
            "inject_logs",
            "distill_logs",
            "consolidation_logs",
            "qzone_logs",
        ):
            try:
                await self.db.execute(f"DELETE FROM {table} WHERE created_at < ?", (cutoff,))
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("清理 %s 失败：%s", table, exc)

    async def _worker_loop(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                if self.memory is not None:
                    for bot_id in await self.memory.bot_ids():
                        due, reason = await self.memory.distiller.due(bot_id)
                        if due:
                            await self.memory.distill(bot_id, reason)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("蒸馏巡检失败：%s", exc)
            await asyncio.sleep(60)

    async def _sweeper_loop(self) -> None:
        interval = max(60, self.cfg("").int("memory.sweeper_seconds", 300, lo=30, hi=3600))
        while True:
            await asyncio.sleep(interval)
            try:
                if self.memory is not None:
                    await self.memory.close_idle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("空闲事件巡检失败：%s", exc)

    # ------------------------------------------------------------------ 钩子
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent) -> None:
        if self.memory is None or self.gate_svc is None:
            return
        try:
            await self._observe(event)
            await self._maybe_gate(event)
        except Exception as exc:  # noqa: BLE001 - 不能因记忆失败打断对话
            self.audit.warn("on_message 失败：%s", exc)

    async def _observe(self, event: AstrMessageEvent) -> None:
        assert self.memory is not None and self.gate_svc is not None
        text = (event.message_str or "").strip()
        if not text:
            return
        bot_id = event.get_platform_id()
        umo = event.unified_msg_origin
        is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
        sender_name = event.get_sender_name()
        self.gate_svc.note_message(umo, sender_name, text)
        if self.session_buffer is not None:
            try:
                await self.session_buffer.record(bot_id, umo, "user", text, sender_name)
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("记录会话消息失败：%s", exc)
        turn = Turn(
            bot_id=bot_id,
            umo=umo,
            role="user",
            text=text,
            ts=clock_mod.now_ms(),
            speaker_id=event.get_sender_id(),
            speaker_name=sender_name,
        )
        await self.memory.observe(
            turn,
            display_name=self._display_name(event),
            chat_type="group" if is_group else "private",
        )

    async def _maybe_gate(self, event: AstrMessageEvent) -> None:
        assert self.gate_svc is not None
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return
        if event.is_at_or_wake_command:
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        cfg = self.cfg(event.get_platform_id())
        if not cfg.bool("interaction.enabled", True):
            return
        if not cfg.bool("interaction.gate_enabled", True):
            return
        decision = await self.gate_svc.judge(
            bot_id=event.get_platform_id(),
            umo=event.unified_msg_origin,
            message=text,
            sender_name=event.get_sender_name(),
            message_id=str(getattr(event.message_obj, "message_id", "") or ""),
        )
        if decision.passed:
            event.is_at_or_wake_command = True
            self.audit.event(
                "gate_pass",
                bot_id=event.get_platform_id(),
                umo=event.unified_msg_origin,
                p=round(decision.probability or 0.0, 3),
            )

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any) -> None:
        try:
            self.audit.set_debug(
                self.cfg(event.get_platform_id()).bool("logging.debug", False)
            )
        except Exception:  # noqa: BLE001
            pass
        if self.memory is None:
            return
        try:
            func_tool = getattr(req, "func_tool", None)
            tools = list(getattr(func_tool, "func_list", []) or []) if func_tool is not None else []
            for tool in tools:
                if getattr(tool, "name", "") in {"say", "speak"}:
                    params = getattr(tool, "parameters", None)
                    if isinstance(params, dict):
                        required = list(params.get("required") or [])
                        if "text" not in required:
                            required.insert(0, "text")
                        params["required"] = required
            event.set_extra("shadow_memory_tool_count", len(tools))
            event.set_extra(
                "shadow_memory_tool_names",
                [str(getattr(tool, "name", "")) for tool in tools],
            )
            event.set_extra(
                "shadow_memory_tool_active_names",
                [
                    str(getattr(tool, "name", ""))
                    for tool in tools
                    if getattr(tool, "active", True)
                ],
            )
            issues: list[str] = []
            schema_bytes = 0
            try:
                schemas = func_tool.openai_schema() if func_tool is not None else []
                schema_bytes = len(json.dumps(schemas, ensure_ascii=False, default=str))
                for item in schemas:
                    func = item.get("function") if isinstance(item, dict) else None
                    if not isinstance(func, dict):
                        issues.append("bad_item")
                        continue
                    name = str(func.get("name") or "?")
                    params = func.get("parameters")
                    if not isinstance(params, dict):
                        issues.append(f"{name}:parameters_missing")
                        continue
                    if params.get("type") != "object":
                        issues.append(f"{name}:type_not_object")
                    props = params.get("properties")
                    if not isinstance(props, dict):
                        issues.append(f"{name}:properties_missing")
                        continue
                    required = params.get("required")
                    if required is not None and not isinstance(required, list):
                        issues.append(f"{name}:required_not_list")
                    elif isinstance(required, list):
                        missing = [key for key in required if key not in props]
                        if missing:
                            issues.append(f"{name}:required_missing={missing}")
            except Exception as exc:  # noqa: BLE001
                issues.append(f"schema_error:{exc}")
            event.set_extra("shadow_memory_schema_issues", issues[:20])
            event.set_extra("shadow_memory_schema_bytes", schema_bytes)
            event.set_extra(
                "shadow_memory_system_chars",
                len(getattr(req, "system_prompt", "") or ""),
            )
            contexts = getattr(req, "contexts", None) or []
            context_summary = []
            for message in contexts[-10:]:
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "?")
                if message.get("tool_calls"):
                    role += "+tool"
                context_summary.append(role)
            event.set_extra("shadow_memory_context_summary", context_summary)
            self.audit.debug(
                "tool_schema_summary",
                bot_id=event.get_platform_id(),
                count=len(tools),
                schema_bytes=schema_bytes,
                system_chars=len(getattr(req, "system_prompt", "") or ""),
                issues=issues[:20],
            )
        except Exception:  # noqa: BLE001
            event.set_extra("shadow_memory_tool_count", 0)
        self._check_provider_modalities(event)
        try:
            await self._inject_context(event, req)
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("固定上下文注入失败：%s", exc)
        try:
            await self._inject(event, req)
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("注入失败：%s", exc)
            self.audit.debug("persona_inject_error", bot_id=event.get_platform_id(), error=str(exc))
        try:
            await self._inject_environment(event, req)
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("交流环境注入失败：%s", exc)
        try:
            self._inject_say_example(event, req)
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("say 示例注入失败：%s", exc)

    @staticmethod
    def _chat_scene(event: AstrMessageEvent) -> tuple[str, str, str]:
        message_type = event.get_message_type()
        if message_type == MessageType.GROUP_MESSAGE:
            group = getattr(getattr(event, "message_obj", None), "group", None)
            group_name = str(getattr(group, "group_name", "") or "") if group else ""
            group_id = str(getattr(event.message_obj, "group_id", "") or "")
            return "group", group_name, group_id
        if message_type == MessageType.FRIEND_MESSAGE:
            return "private", "", ""
        return "other", "", ""

    async def _inject_environment(self, event: AstrMessageEvent, req: Any) -> None:
        cfg = self.cfg(event.get_platform_id())
        if not cfg.bool("interaction.enabled", True):
            return
        if not cfg.bool("interaction.environment.enabled", True):
            return
        chat_type, group_name, group_id = self._chat_scene(event)
        block = build_environment_prompt(
            platform=event.get_platform_name(),
            chat_type=chat_type,
            group_name=group_name,
            group_id=group_id,
            sender_name=event.get_sender_name() or "",
            rules=cfg.str("interaction.environment.prompt", "").strip(),
        )
        if not block:
            return
        current = getattr(req, "system_prompt", "") or ""
        req.system_prompt = (current.rstrip() + "\n\n" + block) if current.strip() else block
        self.audit.event(
            "environment_injected",
            bot_id=event.get_platform_id(),
            chat_type=chat_type,
            chars=len(block),
            preview=block.replace("\n", " | ")[:180],
        )

    def _ltm_settings(self, umo: str) -> dict[str, Any]:
        try:
            config = self.context.get_config(umo=umo) or {}
        except Exception:  # noqa: BLE001
            return {}
        settings = config.get("provider_ltm_settings") if isinstance(config, dict) else None
        return settings if isinstance(settings, dict) else {}

    async def _inject_context(self, event: AstrMessageEvent, req: Any) -> None:
        if self.session_buffer is None or self.kv is None:
            return
        bot_id = event.get_platform_id()
        umo = event.unified_msg_origin
        cfg = self.cfg(bot_id)
        override = await self.kv.get(bot_id, "context_recent_messages")
        limit = resolve_limit(cfg, override)
        if limit <= 0:
            return
        if builtin_group_context_enabled(self._ltm_settings(umo)):
            return
        contexts = getattr(req, "contexts", None)
        if contexts is None:
            contexts = []
            req.contexts = contexts
        if not isinstance(contexts, list):
            return
        recent = await self.session_buffer.last(bot_id, umo, limit)
        recent = without_current(recent, event.message_str or "")
        max_chars = cfg.int("context.max_chars", 2000, lo=200, hi=20000)
        recent = trim_to_chars(recent, max_chars)
        if not recent:
            return
        if contexts:
            missing = missing_for_contexts(contexts, recent)
            if missing:
                contexts[:0] = missing
            return
        block = format_block(recent, max_chars)
        if not block:
            return
        parts = getattr(req, "extra_user_content_parts", None)
        if parts is None:
            parts = []
            req.extra_user_content_parts = parts
        parts.append(TextPart(text=block).mark_as_temp())

    def _inject_say_example(self, event: AstrMessageEvent, req: Any) -> None:
        """在上下文末尾放一条临时 say 工具调用示例，覆盖纯文本 assistant 先例。"""
        cfg = self.cfg(event.get_platform_id())
        if not cfg.bool("interaction.enabled", True):
            return
        if not cfg.bool("interaction.say_example", True):
            return
        func_tool = getattr(req, "func_tool", None)
        names = [
            str(getattr(tool, "name", ""))
            for tool in (getattr(func_tool, "func_list", []) or [])
        ]
        if "say" not in names:
            return
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list):
            return
        for message in contexts[-4:]:
            if (
                isinstance(message, dict)
                and message.get("role") == "assistant"
                and message.get("tool_calls")
            ):
                return
        req.contexts = list(contexts) + say_example_messages()
        event.set_extra("shadow_memory_say_example", True)
        self.audit.debug(
            "say_example_injected",
            bot_id=event.get_platform_id(),
            context_count=len(req.contexts),
        )

    def _check_provider_modalities(self, event: AstrMessageEvent) -> None:
        """AstrBot 在 modalities 非空且缺 tool_use 时会清空 tools，这里提前告警。"""
        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
            if provider is None:
                return
            config = getattr(provider, "provider_config", None) or {}
            modalities = config.get("modalities") if isinstance(config, dict) else None
            try:
                model = str(provider.get_model() or "")
            except Exception:  # noqa: BLE001
                model = str(config.get("model", "")) if isinstance(config, dict) else ""
            event.set_extra("shadow_memory_provider_model", model)
            event.set_extra("shadow_memory_provider_modalities", modalities)
            if isinstance(modalities, list) and modalities and "tool_use" not in modalities:
                key = f"{event.get_platform_id()}:{model}"
                if key not in self._tool_use_warned:
                    self._tool_use_warned.add(key)
                    self.audit.warn(
                        "provider_tool_use_disabled bot=%s model=%s modalities=%s；"
                        "AstrBot 会在请求前清空 tools，请在对话模型的「模型能力」里勾上 tool_use",
                        event.get_platform_id(),
                        model,
                        modalities,
                    )
            else:
                self.audit.debug(
                    "provider_modalities",
                    bot_id=event.get_platform_id(),
                    model=model,
                    modalities=modalities,
                )
        except Exception as exc:  # noqa: BLE001
            self.audit.debug("provider_modalities_check_failed", error=str(exc))

    async def _inject(self, event: AstrMessageEvent, req: Any) -> None:
        assert self.memory is not None
        bot_id = event.get_platform_id()
        umo = event.unified_msg_origin
        cfg = self.cfg(bot_id)
        self.audit.debug(
            "persona_inject_start",
            bot_id=bot_id,
            umo=umo,
            memory_enabled=cfg.bool("memory.enabled", True),
        )
        if not cfg.bool("memory.enabled", True):
            self.audit.debug("persona_inject_skip", bot_id=bot_id, reason="memory_disabled")
            return
        query = (event.message_str or "").strip() or str(getattr(req, "prompt", "") or "").strip()
        if not query:
            self.audit.debug("persona_inject_skip", bot_id=bot_id, reason="empty_query")
            return
        veto = await self._recall_veto(bot_id, umo, query)
        recent = self._context_texts(req)
        prepared = await self.memory.prepare_turn(
            bot_id, umo, query, recent, veto_reason=veto
        )
        block = prepared.persona_text.strip()
        mode = cfg.str("persona.mode", "append")
        system_before = len(getattr(req, "system_prompt", "") or "")
        base_chars = 0
        self.audit.debug(
            "persona_inject_prepared",
            bot_id=bot_id,
            persona_chars=len(block),
            parts=len(prepared.parts),
            reason=prepared.reason,
            veto=veto or "",
            mode=mode,
            system_chars=system_before,
        )
        if block:
            if mode == "plugin_only":
                base = cfg.str("persona.base_card", "").strip()
                base_chars = len(base)
                if base:
                    block = base + "\n" + block
            current = getattr(req, "system_prompt", "") or ""
            pieces = [current.rstrip()] if current.strip() else []
            pieces.append(block)
            if MEMORY_OWNERSHIP_PROMPT not in current:
                pieces.append(MEMORY_OWNERSHIP_PROMPT)
                self.audit.debug(
                    "memory_ownership_prompt_added",
                    bot_id=bot_id,
                    chars=len(MEMORY_OWNERSHIP_PROMPT),
                )
            req.system_prompt = "\n\n".join(pieces)
            self.audit.debug(
                "persona_inject_applied",
                bot_id=bot_id,
                mode=mode,
                base_chars=base_chars,
                persona_chars=len(block),
                system_before=system_before,
                system_after=len(req.system_prompt or ""),
            )
        else:
            current = getattr(req, "system_prompt", "") or ""
            if MEMORY_OWNERSHIP_PROMPT not in current:
                req.system_prompt = (
                    (current.rstrip() + "\n\n" + MEMORY_OWNERSHIP_PROMPT)
                    if current.strip()
                    else MEMORY_OWNERSHIP_PROMPT
                )
                self.audit.debug(
                    "memory_ownership_prompt_added",
                    bot_id=bot_id,
                    chars=len(MEMORY_OWNERSHIP_PROMPT),
                )
            self.audit.debug(
                "persona_inject_skip", bot_id=bot_id, reason="empty_persona"
            )
        recall_block = format_recall_block(
            list(prepared.parts), fact=bool(prepared.fact_ids)
        )
        if recall_block:
            extra_parts = getattr(req, "extra_user_content_parts", None)
            if not isinstance(extra_parts, list):
                extra_parts = []
                req.extra_user_content_parts = extra_parts
            extra_parts.append(TextPart(text=recall_block).mark_as_temp())
            kind = "fact" if prepared.fact_ids else "self"
            self.audit.debug(
                "recall_inject",
                bot_id=bot_id,
                kind=kind,
                parts=len(prepared.parts),
                chars=len(recall_block),
                labels=["我知道"] if kind == "fact" else ["我想起", "我心里还留着"],
            )
        else:
            self.audit.debug("recall_inject_skip", bot_id=bot_id, reason="no_parts")
        self.audit.debug(
            "persona_inject_done",
            bot_id=bot_id,
            recall_parts=len(prepared.parts),
            system_chars=len(getattr(req, "system_prompt", "") or ""),
        )
        event.set_extra(
            PENDING_KEY,
            {
                "query": query,
                "reason": prepared.reason,
                "p_top": prepared.injection.p_top if prepared.injection else 0.0,
                "p_none": prepared.injection.p_none if prepared.injection else 0.0,
                "event_ids": [prepared.injection.main.event_id]
                if prepared.injection and prepared.injection.main
                else [],
                "parts": prepared.parts,
            },
        )

    async def _recall_veto(self, bot_id: str, umo: str, query: str) -> str | None:
        assert self.memory is not None
        if query.startswith(("/", "／")):
            return "veto"
        cfg = self.cfg(bot_id)
        window = cfg.int("recall.no_inject_last_turns", 3, lo=1, hi=20)
        rows = await self.memory.recent_injections(bot_id, umo, window)
        if len(rows) >= window and all(self._has_ids(row.get("event_ids")) for row in rows):
            return "cooldown"
        return None

    @staticmethod
    def _has_ids(raw: Any) -> bool:
        if not raw:
            return False
        if isinstance(raw, list):
            return bool(raw)
        text = str(raw).strip()
        return text not in {"", "[]", "null"}

    @staticmethod
    def _context_texts(req: Any) -> list[str]:
        texts: list[str] = []
        for message in (getattr(req, "contexts", None) or [])[-10:]:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = message.get("content")
            body = ""
            if isinstance(content, str):
                body = content
            elif isinstance(content, list):
                body = " ".join(
                    str(part.get("text", "")) for part in content if isinstance(part, dict)
                )
            body = body.strip()
            if body:
                texts.append(f"{role}: {body[:160]}")
        return texts

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: Any) -> None:
        text = getattr(response, "completion_text", "") or ""
        event.set_extra(LLM_TEXT_KEY, text)
        try:
            tools = list(getattr(response, "tools_call_name", None) or [])
            self.audit.event(
                "llm_response",
                bot_id=event.get_platform_id(),
                tools=tools,
                text_chars=len(text),
            )
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("记录 LLM 工具调用失败：%s", exc)
        if self.db is None or self.memory is None:
            return
        pending = event.get_extra(PENDING_KEY)
        if not pending or event.get_extra(LOGGED_KEY):
            return
        event.set_extra(LOGGED_KEY, True)
        usage = getattr(response, "usage", None)
        cache_hit = int(getattr(usage, "input_cached", 0) or 0)
        cache_other = int(getattr(usage, "input_other", 0) or 0)
        try:
            await self.db.execute(
                "INSERT INTO inject_logs(bot_id, umo, event_ids, persona_version, cache_hit, "
                "cache_other, latency_ms, reason, p_top, p_none, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.get_platform_id(),
                    event.unified_msg_origin,
                    json.dumps(pending.get("event_ids") or [], ensure_ascii=False),
                    await self.memory.persona.version(event.get_platform_id()),
                    cache_hit,
                    cache_other,
                    0,
                    str(pending.get("reason") or ""),
                    float(pending.get("p_top") or 0.0),
                    float(pending.get("p_none") or 0.0),
                    clock_mod.now_ms(),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("写注入日志失败：%s", exc)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        try:
            self._decorate(event)
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("结果装饰失败：%s", exc)

    def _decorate(self, event: AstrMessageEvent) -> None:
        cfg = self.cfg(event.get_platform_id())
        if not cfg.bool("interaction.enabled", True):
            return
        state = state_of(event)
        result = event.get_result()
        if result is not None and getattr(result, "chain", None) is not None:
            is_llm_result = True
            checker = getattr(result, "is_llm_result", None)
            if callable(checker):
                try:
                    is_llm_result = bool(checker())
                except Exception:  # noqa: BLE001
                    is_llm_result = True
            if is_llm_result:
                fallback = cfg.bool("interaction.fallback_send_final_text", False)
                try:
                    tools = int(event.get_extra("shadow_memory_tool_count") or 0)
                except (TypeError, ValueError):
                    tools = 0
                if should_drop_final_text(
                    sent=state.sent, silent=state.silent, fallback=fallback
                ):
                    names = event.get_extra("shadow_memory_tool_names") or []
                    active = event.get_extra("shadow_memory_tool_active_names") or []
                    self.audit.event(
                        "drop_final_text",
                        bot_id=event.get_platform_id(),
                        sent=state.sent,
                        silent=state.silent,
                        fallback=fallback,
                        tools=tools,
                        say_tool="say" in names,
                        say_active="say" in active,
                        tool_names=names[:20],
                        active_tools=active[:20],
                        provider_model=event.get_extra("shadow_memory_provider_model"),
                        modalities=event.get_extra("shadow_memory_provider_modalities"),
                        schema_issues=event.get_extra("shadow_memory_schema_issues"),
                        schema_bytes=event.get_extra("shadow_memory_schema_bytes"),
                        system_chars=event.get_extra("shadow_memory_system_chars"),
                        context_summary=event.get_extra("shadow_memory_context_summary"),
                        say_example=event.get_extra("shadow_memory_say_example"),
                    )
                    result.chain.clear()
                    if not state.sent and not state.silent:
                        event.set_extra("shadow_memory_no_tool", True)
        self._schedule_post_turn(event)

    def _schedule_post_turn(self, event: AstrMessageEvent) -> None:
        if self.memory is None:
            return
        if not event.get_extra(PENDING_KEY):
            return
        if event.get_extra(POST_KEY):
            return
        event.set_extra(POST_KEY, True)
        task = asyncio.create_task(self._safe_post_turn(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _safe_post_turn(self, event: AstrMessageEvent) -> None:
        assert self.memory is not None
        try:
            pending = event.get_extra(PENDING_KEY) or {}
            state = state_of(event)
            assistant = "\n".join([*state.texts, *state.image_texts]).strip() or str(
                event.get_extra(LLM_TEXT_KEY) or ""
            )
            user_text = str(pending.get("query") or "")
            bot_id = event.get_platform_id()
            umo = event.unified_msg_origin
            await self.memory.post_turn(
                bot_id=bot_id,
                umo=umo,
                user_text=user_text,
                assistant_text=assistant,
                speaker_name=event.get_sender_name() or "",
            )
            if self.gate_svc is not None:
                self.gate_svc.note_reply(bot_id, umo)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("post_turn 失败：%s", exc)

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        if self.gate_svc is not None:
            self.gate_svc.note_reply(event.get_platform_id(), event.unified_msg_origin)
        await self._record_assistant(event)

    async def _record_assistant(self, event: AstrMessageEvent) -> None:
        if self.session_buffer is None:
            return
        state = state_of(event)
        texts = [*state.texts, *state.spoken_texts]
        if not texts or event.get_extra("shadow_memory_assistant_recorded"):
            return
        event.set_extra("shadow_memory_assistant_recorded", True)
        for text in texts:
            try:
                await self.session_buffer.record(
                    event.get_platform_id(),
                    event.unified_msg_origin,
                    "assistant",
                    text,
                )
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("记录机器人回复失败：%s", exc)

    @filter.on_astrbot_loaded()
    async def on_loaded(self) -> None:
        await self._refresh_media_tools()
        task = asyncio.create_task(self._delayed_refresh_media_tools())
        self._bg_tasks.append(task)

    async def _delayed_refresh_media_tools(self) -> None:
        """平台适配器可能比插件晚加载；延迟再对齐一次 qzone/media 工具。"""
        try:
            await asyncio.sleep(5)
            await self._refresh_media_tools()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("延迟刷新媒体工具失败：%s", exc)

    @filter.on_platform_loaded()
    async def on_platform_loaded(self) -> None:
        await self._refresh_media_tools()

    async def _refresh_media_tools(self) -> None:
        try:
            instances = self.context.platform_manager.get_insts()
            if not instances:
                # 平台还没加载完；不要据此停用 qzone，等 on_platform_loaded/延迟刷新。
                self.audit.event("media_tool_skip", reason="no_platform_instance")
                return
            has_third_party = False
            for instance in instances:
                try:
                    meta = instance.meta()
                except Exception:  # noqa: BLE001
                    continue
                if getattr(meta, "name", "") == "aiocqhttp":
                    has_third_party = True
                    break
            config = self.cfg("")
            targets = {
                "qzone": has_third_party and config.bool("interaction.qzone.enabled", True),
                "media": config.bool("interaction.media.enabled", True),
                "send_image": config.bool("interaction.send_image.enabled", True),
            }
            states = {
                "qzone": "_qzone_active",
                "media": "_media_active",
                "send_image": "_send_image_active",
            }
            for name, want in targets.items():
                if want == bool(getattr(self, states[name])):
                    continue
                if want:
                    await self.context.activate_llm_tool_async(name)
                else:
                    await self.context.deactivate_llm_tool_async(name)
                setattr(self, states[name], want)
                self.audit.event("media_tool", tool=name, active=want)
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("刷新媒体工具状态失败：%s", exc)

    async def _reconcile_memos(self) -> None:
        if self.memory is None or self.memos is None or self.db is None:
            return
        try:
            jobs = await self.context.cron_manager.list_jobs()
            live = {str(getattr(job, "job_id", "")) for job in jobs}
            for bot_id in await self.memory.bot_ids():
                lost = await self.memos.reconcile(bot_id, live)
                if lost:
                    self.audit.warn("%s 有 %s 条备忘录任务已丢失", bot_id, lost)
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("对齐备忘录任务失败：%s", exc)

    # ------------------------------------------------------------------ 外部依赖
    async def _resolve_decision_provider(self, provider_id: str) -> Any:
        try:
            provider = self.context.get_provider_by_id(provider_id)
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("取判断 provider 失败：%s", exc)
            return None
        if provider is None or not hasattr(provider, "ask"):
            return None
        return provider

    def _build_decision_question(
        self, kind: str, instructions: str, criteria: Any
    ) -> Any:
        from astrbot.api.provider import (
            DecisionBooleanQuestion,
            DecisionChoiceQuestion,
            DecisionScoreQuestion,
        )

        if kind == "noul":
            return DecisionBooleanQuestion(instructions)
        if kind == "choice":
            return DecisionChoiceQuestion(instructions, dict(criteria or {}))
        if kind == "score":
            return DecisionScoreQuestion(instructions, list(criteria or []))
        raise ValueError(f"unknown decision question type: {kind}")

    async def _resolve_text_provider(self, bot_id: str, umo: str) -> Any:
        cfg = self.cfg(bot_id)
        provider_id = cfg.str("text.provider_id").strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider is not None:
                return provider
        try:
            return await self.context.get_using_provider_async(umo or "")
        except Exception:  # noqa: BLE001
            return None

    async def _resolve_vision_provider(self, bot_id: str, umo: str) -> Any:
        cfg = self.cfg(bot_id)
        try:
            settings = self.context.get_config().get("provider_settings", {}) or {}
        except Exception:  # noqa: BLE001
            settings = {}
        provider_id = pick_vision_provider_id(cfg, settings)
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider is not None:
                return provider
        return await self._resolve_text_provider(bot_id, umo)

    async def _resolve_tts(self, bot_id: str, umo: str) -> Any:
        kv_value = None
        if self.kv is not None:
            try:
                kv_value = await self.kv.get(bot_id, "tts_provider_id")
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("读取 TTS 绑定失败：%s", exc)
        mapping = self.cfg(bot_id).section("interaction.tts_provider_ids")
        provider_id = resolve_tts_provider_id(kv_value, mapping, bot_id)
        if not provider_id:
            return None
        provider = self.context.get_provider_by_id(provider_id)
        if provider is not None and hasattr(provider, "get_audio"):
            return provider
        return None

    async def _send_to(self, umo: str, chain: Any) -> bool:
        try:
            return bool(await self.context.send_message(umo, chain))
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("跨会话发送失败：%s", exc)
            return False

    async def _schedule_memo(
        self,
        *,
        name: str,
        cron_expression: str | None,
        payload: dict[str, Any],
        run_once: bool,
        run_at: Any,
    ) -> str:
        job = await self.context.cron_manager.add_active_job(
            name=name,
            cron_expression=cron_expression,
            payload=payload,
            persistent=True,
            run_once=run_once,
            run_at=run_at,
        )
        return str(job.job_id)

    async def _delete_job(self, job_id: str) -> None:
        await self.context.cron_manager.delete_job(job_id)

    def _media_dir(self, bot_id: str) -> MediaDir:
        cfg = self.cfg(bot_id)
        raw = cfg.str("interaction.media.dir", "qzone_media") or "qzone_media"
        path = Path(raw)
        if not path.is_absolute():
            path = self.data_dir / raw
        return MediaDir(
            path,
            bot_id,
            max_images=cfg.int("interaction.qzone.max_images", 9, lo=1, hi=20),
            max_image_mb=cfg.int("interaction.media.max_image_mb", 5, lo=1, hi=50),
            allow_event_images=cfg.bool("interaction.media.allow_event_images", True),
            max_name_chars=cfg.int("interaction.media.max_name_chars", 32, lo=1, hi=64),
        )

    # ------------------------------------------------------------------ 工具
    @filter.llm_tool("say")
    async def tool_say(self, event: AstrMessageEvent, text: str, session: str = "") -> str:
        """发送一条 QQ 消息。要说话时必须调用本工具；普通正文不会发给用户。
        示例：say(text="在吗")

        Args:
            text(string): 要说的一句话（必填）；一轮最多 3 句，一句一句发
            session(string): 可选。只在备忘录提醒轮次使用；目标必须来自会话目录
        """
        if self.sender is None:
            return "现在说不了话。"
        bot_id = event.get_platform_id()
        target = await self._memo_target(event, session)
        if target is False:
            return "那个会话不在这份目录里。"
        return await self.sender.say(
            event, bot_id=bot_id, text=text, target_session=target or None
        )

    @filter.llm_tool("speak")
    async def tool_speak(self, event: AstrMessageEvent, text: str, session: str = "") -> str:
        """发送一条语音消息。想用声音说时才调用；一轮最多 1 次、不超过 60 字。

        Args:
            text(string): 要说的话（必填），不超过 60 字
            session(string): 可选。只在备忘录提醒轮次使用；目标必须来自会话目录
        """
        if self.sender is None:
            return "现在出不了声。"
        bot_id = event.get_platform_id()
        target = await self._memo_target(event, session)
        if target is False:
            return "那个会话不在这份目录里。"
        return await self.sender.speak(
            event,
            bot_id=bot_id,
            umo=event.unified_msg_origin,
            text=text,
            target_session=target or None,
        )

    @filter.llm_tool("silent")
    async def tool_silent(self, event: AstrMessageEvent) -> str:
        """这一轮决定不说话。不要用空的 say 代替；决定不回复时调用。"""
        if self.sender is None:
            return "好"
        return self.sender.silent(event)

    async def _memo_target(self, event: AstrMessageEvent, session: str) -> str | bool:
        target = (session or "").strip()
        if not target:
            return ""
        if not event.get_extra("cron_job"):
            return ""
        if self.catalog is None:
            return False
        if await self.catalog.contains(event.get_platform_id(), target):
            return target
        return False

    @filter.llm_tool("notes")
    async def tool_notes(
        self,
        event: AstrMessageEvent,
        action: str,
        kind: str = "",
        title: str = "",
        content: str = "",
        tags: list[str] | None = None,
        query: str = "",
        id: str = "",
        limit: int = 5,
    ) -> str:
        """你自己的本子。可以写笔记、写日记、翻看以前写的东西。

        Args:
            action(string): write / search / read / list
            kind(string): note 或 diary
            title(string): 标题
            content(string): 正文
            tags(array): 标签
            query(string): 检索词
            id(string): 笔记 id
            limit(number): 返回条数
        """
        if self.notes is None:
            return "本子不见了。"
        bot_id = event.get_platform_id()
        try:
            if action == "write":
                result = await self.notes.write(
                    bot_id,
                    kind=kind or "note",
                    title=title,
                    content=content,
                    tags=tags or [],
                )
                return f"记下了（#{result['id']}）。"
            if action == "search":
                rows = await self.notes.search(bot_id, query, kind or None, limit or 5)
                if not rows:
                    return "没翻到。"
                return "\n".join(
                    f"#{row['id']} [{row['kind']}] {row['title']}：{row['excerpt']}"
                    for row in rows
                )
            if action == "read":
                row = await self.notes.read(bot_id, int(id or 0))
                if not row:
                    return "没有这条。"
                return f"#{row['id']} {row.get('title') or ''}\n{row.get('body') or ''}"
            if action == "list":
                rows = await self.notes.list(bot_id, kind or None, limit or 10)
                if not rows:
                    return "本子是空的。"
                return "\n".join(
                    f"#{row['id']} [{row['kind']}] {row.get('title') or ''}" for row in rows
                )
        except (ValueError, TypeError) as exc:
            return f"没写成：{exc}"
        return "这个动作不认识。"

    @filter.llm_tool("memo")
    async def tool_memo(
        self,
        event: AstrMessageEvent,
        action: str,
        memo_id: str = "",
        note: str = "",
        run_at: str = "",
        cron: str = "",
    ) -> str:
        """给自己设一个提醒。到时间会把你叫醒，你可以决定要不要说、对谁说。

        Args:
            action(string): create / list / edit / delete / get
            memo_id(string): 备忘录 id
            note(string): 提醒内容
            run_at(string): ISO 时间，例如 2026-11-03T08:00:00+08:00
            cron(string): 周期表达式，5 段；与 run_at 二选一
        """
        if self.memos is None:
            return "备忘录不可用。"
        bot_id = event.get_platform_id()
        try:
            if action == "create":
                result = await self.memos.create(
                    bot_id,
                    note=note,
                    umo=event.unified_msg_origin,
                    sender_id=event.get_sender_id(),
                    run_at=run_at or None,
                    cron=cron or None,
                )
                return f"设好了（#{result['memo_id']}）。"
            if action == "list":
                rows = await self.memos.list(bot_id)
                if not rows:
                    return "没有提醒。"
                return "\n".join(self.memos.render(row) for row in rows)
            if action == "delete":
                ok = await self.memos.delete(bot_id, int(memo_id or 0))
                return "删掉了。" if ok else "没有这条。"
            if action == "edit":
                await self.memos.edit(
                    bot_id,
                    int(memo_id or 0),
                    note=note or None,
                    run_at=run_at or None,
                    cron=cron or None,
                )
                return "改好了。"
            if action == "get":
                row = await self.memos.get(bot_id, int(memo_id or 0))
                if row is None:
                    return "没有这条提醒。"
                return await self._memo_context(row, event)
        except (ValueError, TypeError) as exc:
            return f"没成：{exc}"
        return "这个动作不认识。"

    async def _memo_context(self, row: dict[str, Any], event: AstrMessageEvent) -> str:
        bot_id = str(row.get("bot_id") or "")
        cfg = self.cfg(bot_id)
        persona = ""
        catalog = ""
        recent = ""
        environment = ""
        if self.persona is not None:
            try:
                persona = await self.persona.render(bot_id)
            except Exception:  # noqa: BLE001
                persona = ""
        if self.catalog is not None:
            try:
                catalog = await self.catalog.render(bot_id)
            except Exception:  # noqa: BLE001
                catalog = ""
        if self.session_buffer is not None and self.kv is not None:
            try:
                override = await self.kv.get(bot_id, "context_recent_messages")
                limit = resolve_limit(cfg, override)
                if limit > 0:
                    umo = str(row.get("target_session") or event.unified_msg_origin or "")
                    max_chars = cfg.int("context.max_chars", 2000, lo=200, hi=20000)
                    rows = await self.session_buffer.last(bot_id, umo, limit)
                    recent = format_block(trim_to_chars(rows, max_chars), max_chars)
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("读取唤醒上下文失败：%s", exc)
        if cfg.bool("interaction.enabled", True) and cfg.bool(
            "interaction.environment.enabled", True
        ):
            try:
                chat_type, group_name, group_id = self._chat_scene(event)
                environment = build_environment_prompt(
                    platform=event.get_platform_name(),
                    chat_type=chat_type,
                    group_name=group_name,
                    group_id=group_id,
                    sender_name=event.get_sender_name() or "",
                    rules=cfg.str("interaction.environment.prompt", "").strip(),
                    compact=True,
                )
                if environment:
                    self.audit.event(
                        "environment_injected",
                        bot_id=bot_id,
                        source="memo",
                        chars=len(environment),
                        preview=environment.replace("\n", " | ")[:180],
                    )
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("唤醒环境注入失败：%s", exc)
        return (
            f"提醒内容：{row.get('note') or ''}\n"
            f"现在：{clock_mod.iso()}\n\n"
            f"你的人格：\n{persona or '（还没有成形）'}\n\n"
            f"最近的会话：\n{catalog or '（没有可用的会话）'}\n\n"
            f"最近的对话：\n{recent or '（没有记录）'}\n\n"
            f"{environment}\n\n"
            "我想说就调 say / speak，不想说就调 silent。"
        )

    @filter.llm_tool("qzone")
    async def tool_qzone(
        self,
        event: AstrMessageEvent,
        action: str,
        text: str = "",
        images: list[str] | None = None,
        ugc_right: int = 0,
    ) -> str:
        """发一条 QQ 空间说说，或者看看本地图片目录里有哪些图。

        Args:
            action(string): post 发说说 / images 看图片目录
            text(string): 说说正文，像随手发的一条
            images(array): 图片，可以是 http(s) 链接、event:1 这样的本轮图片，或图片目录里的文件名
            ugc_right(number): 可见范围 1 所有人 / 4 好友 / 64 仅自己
        """
        if self.qzone is None:
            return "空间不可用。"
        bot_id = event.get_platform_id()
        if action == "images":
            rows = await self.qzone.list_images(bot_id)
            if not rows:
                return "图片目录是空的。"
            return "\n".join(f"{row['name']}（{row['size_kb']} KB）" for row in rows)
        if action != "post":
            return "这个动作不认识。"
        result = await self.qzone.post(
            event,
            bot_id=bot_id,
            text=text,
            images=images or [],
            ugc_right=ugc_right or None,
        )
        return str(result.get("message") or "")

    @filter.llm_tool("media")
    async def tool_media(
        self,
        event: AstrMessageEvent,
        action: str,
        source: str = "",
        name: str = "",
        description: str = "",
        tags: list[str] | None = None,
        filename: str = "",
        query: str = "",
        limit: int = 5,
    ) -> str:
        """你自己的图片库：把图存起来、翻看、按印象找、删掉。

        Args:
            action(string): save 存一张图 / list 最近存的 / search 按印象找 / delete 删一张
            source(string): save 用；event:1 表示这条消息里的第 1 张图，或 http(s) 链接
            name(string): save 用；可选名字提示，作为文件名前缀
            description(string): save 用；你看得到这张图就写你看到的；看不到就留空，插件会自己看图补一句
            tags(array): save 用；几个关键词
            filename(string): delete 用；只能写 list/search 看到的文件名
            query(string): search 用；你记得的模糊印象
            limit(number): list/search 返回条数
        """
        if self.media_lib is None:
            return "图片库不可用。"
        bot_id = event.get_platform_id()
        if action == "save":
            result = await self.media_lib.save(
                event,
                bot_id=bot_id,
                source=source,
                name=name,
                description=description,
                tags=tags,
            )
            return str(result.get("message") or "")
        if action == "list":
            rows = await self.media_lib.list(bot_id, limit or 10)
            if not rows:
                return "图片目录还是空的。"
            return "\n".join(self._format_media_row(row) for row in rows)
        if action == "search":
            rows = await self.media_lib.search(bot_id, query, limit or 5)
            if not rows:
                return "没找到像这样的图。"
            return "\n".join(self._format_media_row(row) for row in rows)
        if action == "delete":
            result = await self.media_lib.delete(bot_id, filename)
            return str(result.get("message") or "")
        return "这个动作不认识。"

    @staticmethod
    def _format_media_row(row: dict[str, Any]) -> str:
        description = row.get("description") or "（没写描述）"
        sender = f"｜{row['sender_name']}" if row.get("sender_name") else ""
        return f"{row.get('filename') or ''}｜{description}{sender}｜{row.get('size_kb') or 0} KB"

    @filter.llm_tool("send_image")
    async def tool_send_image(
        self, event: AstrMessageEvent, source: str, caption: str = "", session: str = ""
    ) -> str:
        """发一张图片。可以带一句话，像把照片递过去那样。

        Args:
            source(string): 要发的图：event:1 / 图片库文件名 / http(s) 链接
            caption(string): 可选，和图片一起发的一句话
            session(string): 可选；只在备忘录提醒轮次使用，目标必须来自会话目录
        """
        if self.media_lib is None or self.sender is None:
            return "现在发不了图。"
        bot_id = event.get_platform_id()
        limit = self.cfg(bot_id).int("interaction.send_image.max_per_turn", 2, lo=0, hi=10)
        if state_of(event).images >= limit:
            return f"这一轮已经发过 {limit} 张图了。"
        target = await self._memo_target(event, session)
        if target is False:
            return "那个会话不在这份目录里。"
        try:
            resolved = await self.media_lib.resolve_for_send(event, bot_id=bot_id, source=source)
        except MediaError as exc:
            return str(exc)
        filename = resolved.get("filename")
        before = state_of(event).images
        result = await self.sender.send_image(
            event,
            bot_id=bot_id,
            data=resolved["data"],
            caption=caption,
            target_session=target or None,
        )
        if state_of(event).images > before:
            if filename:
                await self.media_lib.mark_used(bot_id, filename)
            await self.media_lib.log_send(bot_id, filename, "ok")
        else:
            await self.media_lib.log_send(bot_id, filename, "rejected", result)
        return result

    @filter.llm_tool("memory_search")
    async def tool_memory_search(
        self, event: AstrMessageEvent, query: str, limit: int = 5
    ) -> str:
        """查一查你记着的关于对方或世界的事实。只在需要确认细节时用。

        Args:
            query(string): 要查的关键词
            limit(number): 最多返回几条
        """
        if self.facts is None:
            return "想不起来了。"
        rows = await self.facts.search(event.get_platform_id(), query, limit or 5)
        if not rows:
            return "没查到。"
        return "\n".join(f"- {row['text']}" for row in rows)

    # ------------------------------------------------------------------ 命令
    async def _admin_private(self, event: AstrMessageEvent) -> bool:
        return event.is_private_chat()

    @filter.command("memory")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def memory_cmd(self, event: AstrMessageEvent):
        """查看或管理当前机器人的记忆（仅管理员、仅私聊）。"""
        if not await self._admin_private(event):
            yield event.plain_result("这个命令只能在私聊里用。")
            return
        if self.memory is None or self.db is None:
            yield event.plain_result("记忆模块还没准备好。")
            return
        bot_id = event.get_platform_id()
        text = self._command_args(event, "memory")
        parts = text.split()
        action = parts[0] if parts else ""
        try:
            if action == "":
                data = await self.memory.summary(bot_id)
                if self.media_lib is not None:
                    data["media"] = await self.media_lib.stats(bot_id)
                yield event.plain_result(self._format_summary(data))
                return
            if action == "export":
                kind = parts[1] if len(parts) > 1 else "seed"
                payload = (
                    await self.memory.export_seed(bot_id)
                    if kind != "full"
                    else await self.memory.export_full(bot_id)
                )
                path = self._export_path(bot_id, kind)
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                yield event.plain_result(f"已导出：{path}")
                return
            if action == "import":
                if len(parts) < 2:
                    yield event.plain_result("用法：/memory import <文件名> [force]")
                    return
                force = len(parts) > 2 and parts[2].lower() == "force"
                payload = self._read_seed(parts[1])
                counts = await self.memory.import_seed(bot_id, payload, force=force)
                yield event.plain_result(
                    f"导入完成：人格 {counts['persona']} 条，事实 {counts['facts']} 条，情景 {counts['events']} 条。"
                )
                return
            if action == "restore":
                if len(parts) < 2:
                    yield event.plain_result("用法：/memory restore <文件名> [force]")
                    return
                force = len(parts) > 2 and parts[2].lower() == "force"
                payload = self._read_backup(parts[1])
                backup = None
                if force:
                    safe = "".join(
                        char if char.isalnum() or char in "-_." else "_"
                        for char in bot_id
                    )
                    backup = (
                        self.data_dir
                        / "exports"
                        / f"backup_{safe}_{clock_mod.now_ms()}.db"
                    )
                counts = await self.memory.restore_full(
                    bot_id, payload, force=force, backup_path=backup
                )
                suffix = f"；覆盖前备份：{backup}" if backup else ""
                yield event.plain_result(
                    f"恢复完成：人格 {counts['persona']} 条，事实 {counts['facts']} 条，"
                    f"情景 {counts['events']} 条{suffix}"
                )
                return
            if action == "distill":
                result = await self.memory.distill(bot_id, "manual")
                yield event.plain_result(
                    f"蒸馏：应用 {result.applied} 条，拒绝 {len(result.rejected)} 条"
                    + (f"（{result.skipped}）" if result.skipped else "")
                )
                return
            if action == "consolidate":
                result = await self.memory.consolidate(bot_id)
                yield event.plain_result(
                    f"巩固：合并 {result.merged} 簇，改写事实 {result.superseded} 条，洞察 {result.insights} 条，失败 {result.failed} 簇。"
                )
                return
            if action == "decay":
                result = await self.memory.decay(bot_id)
                yield event.plain_result(
                    f"衰减：{result['events']} 条事件，归档 {result['archived']} 条。"
                )
                return
        except SeedError as exc:
            yield event.plain_result(f"被拒绝：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"出错了：{exc}")
            return
        yield event.plain_result("用法：/memory [export [seed|full] | import <文件> [force] | restore <文件> [force] | distill | consolidate | decay]")

    @filter.command("forget")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def forget_cmd(self, event: AstrMessageEvent):
        """删掉一条事件记忆（仅管理员、仅私聊）。"""
        if not await self._admin_private(event):
            yield event.plain_result("这个命令只能在私聊里用。")
            return
        if self.memory is None:
            yield event.plain_result("记忆模块还没准备好。")
            return
        try:
            event_id = int(self._command_args(event, "forget"))
        except ValueError:
            yield event.plain_result("用法：/forget <event_id>")
            return
        ok = await self.memory.forget(event.get_platform_id(), event_id)
        yield event.plain_result("删掉了。" if ok else "没有这条事件。")

    @filter.command("persona")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def persona_cmd(self, event: AstrMessageEvent):
        """回滚人格到某个版本（仅管理员、仅私聊）。"""
        if not await self._admin_private(event):
            yield event.plain_result("这个命令只能在私聊里用。")
            return
        if self.memory is None:
            yield event.plain_result("记忆模块还没准备好。")
            return
        parts = self._command_args(event, "persona").split()
        if len(parts) != 2 or parts[0] != "rollback":
            yield event.plain_result("用法：/persona rollback <version>")
            return
        try:
            version = int(parts[1])
        except ValueError:
            yield event.plain_result("version 必须是数字。")
            return
        ok = await self.memory.rollback(event.get_platform_id(), version)
        yield event.plain_result("回滚好了。" if ok else "没有这个版本。")

    @filter.command("qzone")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def qzone_cmd(self, event: AstrMessageEvent):
        """手动发一条说说（仅管理员、仅私聊）。"""
        if not await self._admin_private(event):
            yield event.plain_result("这个命令只能在私聊里用。")
            return
        if self.qzone is None:
            yield event.plain_result("空间不可用。")
            return
        body = self._command_args(event, "qzone")
        if not body:
            yield event.plain_result("用法：/qzone <正文>")
            return
        result = await self.qzone.post(
            event, bot_id=event.get_platform_id(), text=body, bypass_limit=True
        )
        yield event.plain_result(str(result.get("message") or ""))

    # ------------------------------------------------------------------ 辅助
    @staticmethod
    def _command_args(event: AstrMessageEvent, name: str) -> str:
        """从 message_str 里剥掉命令本身，保留剩余原文（含空格）。"""
        return command_args(event.message_str, name)

    @staticmethod
    def _display_name(event: AstrMessageEvent) -> str:
        group = getattr(event.message_obj, "group", None)
        if group is not None:
            return str(
                getattr(group, "group_name", "") or getattr(group, "group_id", "") or ""
            )
        return str(event.get_sender_name() or "")

    def _export_path(self, bot_id: str, kind: str) -> Path:
        safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in bot_id)
        folder = self.data_dir / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{safe}_{kind}_{clock_mod.now_ms()}.json"

    def _read_backup(self, filename: str) -> Any:
        name = Path(filename).name
        if name != filename or "/" in filename or "\\" in filename:
            raise SeedError("文件名不合法")
        for folder in ("exports", "seed"):
            path = self.data_dir / folder / name
            if path.is_file():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError) as exc:
                    raise SeedError(f"读取备份失败：{exc}") from exc
        raise SeedError(f"找不到备份文件：{name}")

    def _read_seed(self, filename: str) -> Any:
        name = Path(filename).name
        if name != filename or "/" in filename or "\\" in filename:
            raise SeedError("文件名不合法")
        path = self.data_dir / "seed" / name
        if not path.is_file():
            raise SeedError(f"找不到 seed 文件：{name}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise SeedError(f"seed 读不出来：{exc}") from exc

    @staticmethod
    def _format_summary(data: dict[str, Any]) -> str:
        persona = data.get("persona") or {}
        counts = persona.get("counts") or {}
        sr = data.get("sr") or {}
        inject = data.get("inject_24h") or {}
        gate = data.get("gate_24h") or {}
        events = data.get("events") or []
        event_text = ", ".join(
            f"{row.get('kind')}/{row.get('status')}:{row.get('n')}" for row in events
        ) or "无"
        cached = float(inject.get("cached") or 0)
        total = float(inject.get("total_tokens") or 0)
        hit_rate = f"{cached / total * 100:.0f}%" if total else "—"
        lines = [
            f"机器人：{data.get('bot_id')}",
            f"人格：v{persona.get('version')}，{persona.get('chars')} 字"
            f"（上限 {persona.get('limits', {}).get('hard')}）"
            f" core {counts.get('core')}/edge {counts.get('edge')}/tension {counts.get('tension')}",
            f"事件：{event_text}",
            f"S/R：{float(sr.get('n') or 0):.0f} 条，平均 R {float(sr.get('r') or 0):.2f}，"
            f"平均 S {float(sr.get('s') or 0):.1f} 天",
            f"边：{data.get('edges', {}).get('edges')}，实体：{data.get('edges', {}).get('entities')}",
            f"事实：{data.get('facts', {})}，未用洞察：{data.get('insights_unused')}",
        ]
        media = data.get("media")
        if media:
            lines.append(
                f"媒体：{media.get('files', 0)} 张 / "
                f"{float(media.get('bytes', 0)) / 1048576.0:.1f} MB"
                f"（{media.get('described', 0)} 条有描述）"
            )
        lines += [
            f"24h 注入：{int(inject.get('n') or 0)} 次，命中 {int(inject.get('hit') or 0)} 次，"
            f"平均 p {float(inject.get('p') or 0):.2f}，缓存命中 {hit_rate}",
            f"24h 门控：{int(gate.get('n') or 0)} 条，回复 {int(gate.get('replied') or 0)} 条",
            f"判断：{data.get('judge', {})}",
            f"写作：{data.get('text', {})}",
        ]
        consolidation = data.get("consolidation") or []
        if consolidation:
            last = consolidation[0]
            lines.append(
                f"最近巩固：{last.get('created_at')} / {last.get('status')} / gist {last.get('gist_id')}"
            )
        distill = data.get("distill") or []
        if distill:
            last = distill[0]
            lines.append(
                f"最近蒸馏：{last.get('created_at')} / 应用 {last.get('applied')} / 拒绝 {last.get('rejected')}"
            )
        return "\n".join(lines)


__all__ = ["Main"]
