"""故事模块在 main.py 里的接线。

测试环境装不了 AstrBot，这里用最小替身顶替 ``astrbot`` 的几个模块，把 main.py 真正
导入并跑起来：装配、人设与 provider 解析、/story 命令、状态卡注入、WebUI 路由。
它验证的是接线没有写错，不代表在真实 AstrBot 进程里一定能加载。
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
import types
from types import SimpleNamespace

import pytest

from ..fakes import FakeEvent

MAIN_MODULE = "astrbot_plugin_shadow_memory.main"
BUILTIN_PERSONA = {"name": "default", "prompt": "You are a helpful and friendly assistant."}

SEED = {
    "version": "world_seed/v1",
    "title": "青屿",
    "outline": "她是青屿大学的大三学生，住在学校北门外的合租屋里，日子过得平常。" * 2,
    "entries": [{"category": "人物", "name": "阿澄", "body": "她的室友，学美术。"}],
}
SCENE_OUTPUTS = [
    {"backfill": "", "narration": "你站在便利店门口，雨下大了。", "lookup": []},
    {"inner": "不想淋雨", "action": "我走进便利店，想买把伞。"},
    {"check": None, "narration": "店里只剩一把黑伞，你买下了它。", "lookup": [], "end": True},
    {
        "epilogue": "",
        "card": {"where": "便利店", "doing": "躲雨", "minutes": 40, "phone": "方便"},
        "next_wake_minutes": 45,
        "canon": [],
        "notes": [],
        "summary": "她买了伞。",
    },
    {"fragment": "我在便利店买到了最后一把伞", "entities": ["便利店", "伞"], "summary": ""},
]


class ScriptedProvider:
    def __init__(self, name: str = "default") -> None:
        self.name = name
        self.outputs: list[dict] = []
        self.requests: list[dict] = []

    async def text_chat(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            completion_text=json.dumps(self.outputs.pop(0), ensure_ascii=False)
        )


class StubContext:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, tuple[str, ...]], object] = {}
        self.default_provider = ScriptedProvider()
        self.providers: dict[str, object] = {}
        self.providers_by_umo: dict[str, object] = {}
        self.provider_umos: list[str] = []
        self.persona = dict(BUILTIN_PERSONA)
        self.personas_by_umo: dict[str, dict] = {}
        self.persona_umos: list[str | None] = []
        self.configs_by_umo: dict[str, dict] = {}
        self.config_umos: list[str | None] = []

        async def list_jobs(*args, **kwargs):
            return []

        async def add_basic_job(**kwargs):
            return SimpleNamespace(job_id=f"job-{kwargs.get('name')}")

        async def delete_job(job_id):
            return None

        async def default_persona(umo=None):
            self.persona_umos.append(umo)
            return self.personas_by_umo.get(umo or "", self.persona)

        self.cron_manager = SimpleNamespace(
            list_jobs=list_jobs, add_basic_job=add_basic_job, delete_job=delete_job
        )
        self.platform_manager = SimpleNamespace(get_insts=lambda: [])
        self.persona_manager = SimpleNamespace(get_default_persona_v3=default_persona)

    def register_web_api(self, route, handler, methods, desc) -> None:
        self.routes[(route, tuple(methods))] = handler

    def get_provider_by_id(self, provider_id):
        return self.providers.get(provider_id)

    async def get_using_provider_async(self, umo=""):
        self.provider_umos.append(umo)
        return self.providers_by_umo.get(umo, self.default_provider)

    def get_using_provider(self, umo=""):
        return self.providers_by_umo.get(umo, self.default_provider)

    def get_config(self, umo=None):
        self.config_umos.append(umo)
        return self.configs_by_umo.get(umo or "", {})

    def get_all_tts_providers(self):
        return []


class _Filter:
    """所有装饰器都原样返回被装饰的函数。"""

    class EventMessageType:
        ALL = "all"

    class PermissionType:
        ADMIN = "admin"

    def __getattr__(self, name):
        def factory(*args, **kwargs):
            return lambda fn: fn

        return factory


@pytest.fixture
def plugin_main(monkeypatch, workdir):
    """装上 astrbot 替身并导入 main.py；用完把替身和 main 模块都清掉。"""
    request = SimpleNamespace(username="admin", query={}, payload={})

    async def request_json(default=None):
        return request.payload

    request.json = request_json

    def module(name: str, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    class Star:
        def __init__(self, context, config=None) -> None:
            self.context = context

    class TextPart:
        def __init__(self, text: str = "") -> None:
            self.text = text

        def mark_as_temp(self):
            return self

    module("astrbot")
    module("astrbot.api", logger=logging.getLogger("astrbot-stub"))
    class MessageChain:
        def __init__(self, chain=None) -> None:
            self.chain = list(chain or [])

    module(
        "astrbot.api.event",
        AstrMessageEvent=object,
        filter=_Filter(),
        MessageChain=MessageChain,
    )
    module(
        "astrbot.api.message_components",
        Image=SimpleNamespace(fromBytes=lambda data: ("image", data)),
        Plain=lambda text: SimpleNamespace(text=text),
        Record=lambda file: SimpleNamespace(file=file),
    )
    module(
        "astrbot.api.platform",
        MessageType=SimpleNamespace(
            GROUP_MESSAGE="GroupMessage", FRIEND_MESSAGE="FriendMessage"
        ),
    )
    module(
        "astrbot.api.star",
        Context=object,
        Star=Star,
        StarTools=SimpleNamespace(get_data_dir=lambda name: workdir / "plugin_data"),
    )
    module(
        "astrbot.api.web",
        error_response=lambda message, *, status_code=400, **kwargs: {
            "error": message,
            "status": status_code,
        },
        file_response=lambda path, **kwargs: {"file": str(path), **kwargs},
        json_response=lambda data: data,
        request=request,
    )
    module("astrbot.core")
    module("astrbot.core.agent")
    module("astrbot.core.agent.message", TextPart=TextPart)
    module("astrbot.core.persona_mgr", DEFAULT_PERSONALITY=BUILTIN_PERSONA)

    sys.modules.pop(MAIN_MODULE, None)
    try:
        yield importlib.import_module(MAIN_MODULE), request
    finally:
        sys.modules.pop(MAIN_MODULE, None)


async def make_plugin(plugin_main, config: dict):
    module, _ = plugin_main
    context = StubContext()
    plugin = module.Main(context, config)
    await plugin.initialize()
    for task in plugin._bg_tasks:  # noqa: SLF001 - 测试里不需要后台循环
        task.cancel()
    return plugin, context


def private_event(text: str) -> FakeEvent:
    return FakeEvent(text, message_type="FriendMessage", group=False, session_id="u1")


async def run_command(plugin, text: str) -> list[str]:
    return [result.chain[0].text async for result in plugin.story_cmd(private_event(text))]


# --------------------------------------------------------------------- 人设与 provider
async def test_base_card_resolution_order(plugin_main):
    plugin, context = await make_plugin(plugin_main, {})
    try:
        # AstrBot 内置的通用默认人格不算人设
        assert await plugin._resolve_base_card("bot") == ("", "")
        context.persona = {"name": "小影", "prompt": "她叫小影，话不多。"}
        assert await plugin._resolve_base_card("bot") == ("她叫小影，话不多。", "AstrBot 默认人格「小影」")

        plugin.astrbot_config = {"persona": {"mode": "plugin_only", "base_card": "插件人设"}}
        assert await plugin._resolve_base_card("bot") == ("插件人设", "persona.base_card")
        # append 模式下 persona.base_card 不生效，仍取 AstrBot 默认人格
        plugin.astrbot_config = {"persona": {"mode": "append", "base_card": "插件人设"}}
        assert (await plugin._resolve_base_card("bot"))[1] == "AstrBot 默认人格「小影」"

        plugin.astrbot_config = {
            "persona": {"mode": "plugin_only", "base_card": "插件人设"},
            "story": {"base_card": "故事专用人设"},
            "bot_overrides": {"bot2": {"story_base_card": "二号机的人设"}},
        }
        assert await plugin._resolve_base_card("bot") == ("故事专用人设", "story.base_card")
        assert await plugin._resolve_base_card("bot2") == ("二号机的人设", "story.base_card")
    finally:
        await plugin.terminate()


async def test_story_provider_never_falls_back_silently(plugin_main):
    plugin, context = await make_plugin(
        plugin_main, {"story": {"provider_id": "gm-model", "actor_provider_id": ""}}
    )
    try:
        # 配了 ID 却找不到：返回 None，而不是悄悄把世界设定发给默认模型
        assert await plugin._resolve_story_provider("gm", "bot") is None
        gm = ScriptedProvider("gm")
        context.providers["gm-model"] = gm
        assert await plugin._resolve_story_provider("gm", "bot") is gm
        # 没配的角色用默认对话模型
        assert await plugin._resolve_story_provider("actor", "bot") is context.default_provider
    finally:
        await plugin.terminate()


async def test_bot_routed_persona_and_config_lookup(plugin_main, monkeypatch):
    plugin, context = await make_plugin(plugin_main, {})
    try:
        other_umo = "Inwind_NapCat:OtherMessage:__shadow_bot__"
        friend_umo = "Inwind_NapCat:FriendMessage:__shadow_bot__"

        # 全局 default 配置仍是内置助手，但 Inwind_NapCat 独立配置里有默认人格
        context.personas_by_umo[other_umo] = {"name": "离光", "prompt": "她住在离光之外。"}
        context.personas_by_umo[friend_umo] = {"name": "私聊分流", "prompt": "私聊专用人设。"}
        assert await plugin._resolve_base_card("other_bot") == ("", "")
        assert await plugin._resolve_base_card("Inwind_NapCat") == (
            "她住在离光之外。",
            "AstrBot 默认人格「离光」",
        )

        # 机器人兜底配置仍是内置助手时，回退尝试私聊通配路由
        context.personas_by_umo[other_umo] = dict(BUILTIN_PERSONA)
        assert await plugin._resolve_base_card("Inwind_NapCat") == (
            "私聊专用人设。",
            "AstrBot 默认人格「私聊分流」",
        )

        # 默认对话模型、无会话后台文本模型、视觉打标配置都走该机器人的路由 UMO
        routed_chat = ScriptedProvider("inwind-chat")
        routed_vision = ScriptedProvider("inwind-vision")
        context.providers["inwind-vision"] = routed_vision
        context.providers_by_umo[other_umo] = routed_chat
        context.configs_by_umo[other_umo] = {
            "provider_settings": {
                "default_image_caption_provider_id": "inwind-vision",
                "web_search": True,
                "websearch_provider": "tavily",
            }
        }

        assert await plugin._resolve_story_provider("actor", "Inwind_NapCat") is routed_chat
        assert await plugin._resolve_text_provider("Inwind_NapCat", "") is routed_chat
        assert await plugin._resolve_vision_provider("Inwind_NapCat", "") is routed_vision

        # 故事网页搜索在启用判断与实际执行时都携带同一条机器人探测 UMO
        seen_tool_umos: list[str] = []

        class _FakeTool:
            name = "web_search"

            async def call(self, ctx, **kwargs):
                seen_tool_umos.append(ctx.context.event.unified_msg_origin)
                return f"searched:{kwargs.get('query', '')}"

        class _FakeToolSet:
            def __init__(self) -> None:
                self._tools: dict[str, object] = {}

            def add_tool(self, tool) -> None:
                self._tools[tool.name] = tool

            def get_tool(self, name: str):
                return self._tools.get(name)

            def empty(self) -> bool:
                return not self._tools

        fake_tool = _FakeTool()
        context.get_llm_tool_manager = lambda: SimpleNamespace(
            get_builtin_tool=lambda cls: fake_tool if cls is _FakeTool else None
        )
        tool_mod = types.ModuleType("astrbot.core.agent.tool")
        tool_mod.ToolSet = _FakeToolSet
        ws_mod = types.ModuleType("astrbot.core.tools.web_search_tools")
        ws_mod.normalize_legacy_web_search_config = lambda cfg: None
        ws_mod.TavilyWebSearchTool = _FakeTool
        ws_mod.__getattr__ = lambda name: type(name, (), {})
        monkeypatch.setitem(sys.modules, "astrbot.core.agent.tool", tool_mod)
        monkeypatch.setitem(sys.modules, "astrbot.core.tools", types.ModuleType("astrbot.core.tools"))
        monkeypatch.setitem(sys.modules, "astrbot.core.tools.web_search_tools", ws_mod)

        assert plugin._story_web_search("other_bot") is None
        bundle = plugin._story_web_search("Inwind_NapCat")
        assert bundle is not None
        assert await bundle.run("web_search", {"query": "天气"}) == "searched:天气"
        assert seen_tool_umos == [other_umo]
    finally:
        await plugin.terminate()


# --------------------------------------------------------------------- 命令到记忆的整条链
async def test_story_command_flow_and_chat_injection(plugin_main):
    plugin, context = await make_plugin(
        plugin_main, {"story": {"enabled": True, "base_card": "她叫小影，话不多。"}}
    )
    try:
        bot_id = private_event("").get_platform_id()
        assert "还没有导入世界种子" in (await run_command(plugin, "/story"))[0]
        assert "世界种子" in (await run_command(plugin, "/story on"))[0]  # 没种子不让开

        seed_dir = plugin.data_dir / "seed"
        seed_dir.mkdir(parents=True, exist_ok=True)
        (seed_dir / "world.json").write_text(json.dumps(SEED, ensure_ascii=False), encoding="utf-8")
        assert (await run_command(plugin, "/story import world.json"))[0] == (
            "世界种子已导入：「青屿」，设定 1 条，主线钩子 0 条。"
        )
        assert "找不到" in (await run_command(plugin, "/story import nope.json"))[0]
        opened = (await run_command(plugin, "/story on"))[0]
        assert opened.startswith("故事开启了。") and "人设来源：story.base_card" in opened

        context.default_provider.outputs = [dict(item) for item in SCENE_OUTPUTS]
        replies = await run_command(plugin, "/story wake")
        assert replies[0].startswith("开始演一场")
        assert "她记下的：我在便利店买到了最后一把伞" in replies[1]
        assert context.default_provider.outputs == []

        event = await plugin.db.fetchone("SELECT * FROM events WHERE source='story'")
        assert event["bot_id"] == bot_id and event["fragment"] == "我在便利店买到了最后一把伞"
        status = (await run_command(plugin, "/story status"))[0]
        assert "故事：运行中" in status and "场景 1 场" in status
        assert "状态卡：我现在在便利店，正在躲雨" in status

        # 聊天时，状态卡并入交流环境块的末尾
        req = SimpleNamespace(system_prompt="已有的系统提示词")
        await plugin._inject_environment(private_event("在干嘛"), req)
        assert req.system_prompt.startswith("已有的系统提示词\n\n# 我这会儿")
        tail = req.system_prompt.split("# 我自己这边\n")[1]
        assert tail.startswith("- 我现在在便利店，正在躲雨，大概到")
        assert "不当成已经发生的来说" in tail

        # 聊天请求里：规则在人格前，状态卡在整段系统提示的最后
        req = SimpleNamespace(system_prompt="已有的系统提示词")
        await plugin.on_llm_request(private_event("在干嘛"), req)
        text = req.system_prompt
        assert text.index("# 我这会儿") < text.index("# 关于我的记忆") < text.index("# 我自己这边")
        assert text.index("必须调用 say") < text.index("# 关于我的记忆")

        assert (await run_command(plugin, "/story pause"))[0].startswith("故事暂停了")
        assert "已暂停（手动暂停）" in (await run_command(plugin, "/story status"))[0]
        assert (await run_command(plugin, "/story resume"))[0].startswith("故事继续。")
        assert (await run_command(plugin, "/story off"))[0].startswith("故事关掉了")
        # 关掉后不再往聊天里注入
        req = SimpleNamespace(system_prompt="")
        await plugin._inject_environment(private_event("在干嘛"), req)
        assert "# 我自己这边" not in req.system_prompt
        assert (await run_command(plugin, "/story nonsense"))[0].startswith("用法：/story")
    finally:
        await plugin.terminate()


async def test_story_command_is_private_only(plugin_main):
    plugin, _ = await make_plugin(plugin_main, {"story": {"enabled": True}})
    try:
        replies = [
            result.chain[0].text
            async for result in plugin.story_cmd(FakeEvent("/story on"))  # 群聊
        ]
        assert replies == ["这个命令只能在私聊里用。"]
    finally:
        await plugin.terminate()


# --------------------------------------------------------------------- WebUI 路由
async def test_story_web_routes(plugin_main):
    plugin, context = await make_plugin(
        plugin_main, {"story": {"enabled": True, "base_card": "她叫小影，话不多。"}}
    )
    _, request = plugin_main
    try:
        assert ("/shadow_memory/story", ("GET",)) in context.routes
        assert ("/shadow_memory/story/action", ("POST",)) in context.routes

        request.payload = {"bot": "aiocqhttp", "payload": SEED}
        async def body():
            return json.dumps(request.payload, ensure_ascii=False).encode("utf-8")
        request.body = body
        imported = await plugin.web_import()
        assert imported["kind"] == "world_seed" and imported["counts"]["entries"] == 1

        request.payload = {"bot": "aiocqhttp", "action": "wake"}
        assert "还没开启" in (await plugin.web_story_action())["error"]
        request.payload = {"bot": "aiocqhttp", "action": "on"}
        assert (await plugin.web_story_action())["enabled"] is True

        context.default_provider.outputs = [dict(item) for item in SCENE_OUTPUTS]
        request.payload = {"bot": "aiocqhttp", "action": "wake"}
        assert await plugin.web_story_action() == {"bot_id": "aiocqhttp", "started": True}
        for task in list(plugin._tasks):  # noqa: SLF001 - 等后台那一场演完
            await task

        overview = await plugin.web_story()
        assert overview["master_enabled"] is True
        row = next(item for item in overview["bots"] if item["bot_id"] == "aiocqhttp")
        assert row["scenes_total"] == 1
        assert row["scenes"][0]["fragment"] == "我在便利店买到了最后一把伞"

        request.payload = {"bot": "aiocqhttp", "action": "explode"}
        assert "action 只能是" in (await plugin.web_story_action())["error"]

        request.query = {"bot": "aiocqhttp", "kind": "story"}
        exported = await plugin.web_export()
        data = json.loads(open(exported["file"], encoding="utf-8").read())
        assert data["version"] == "shadow_story/export/v1" and len(data["scenes"]) == 1

        request.username = None
        assert (await plugin.web_story())["status"] == 401
    finally:
        await plugin.terminate()


async def test_aiocqhttp_offline_warning_and_empty_exception_send_log(plugin_main, caplog):
    plugin, context = await make_plugin(
        plugin_main, {"story": {"enabled": True, "base_card": "她叫映风。"}}
    )
    _, request = plugin_main
    try:
        cq_bot = SimpleNamespace(_wsr_api_clients={}, _wsr_event_clients=set())
        inst = SimpleNamespace(
            meta=lambda: SimpleNamespace(id="Inwind_NapCat", name="aiocqhttp"),
            status="running",
            bot=cq_bot,
        )
        context.platform_manager = SimpleNamespace(get_insts=lambda: [inst])

        assert "反向 WebSocket 尚未连接" in plugin._platform_issue("Inwind_NapCat")
        assert "未启用或未加载" in plugin._platform_issue("other_bot")
        cq_bot._wsr_event_clients = {"event_ws"}
        assert "/ws/event" in plugin._platform_issue("Inwind_NapCat")
        cq_bot._wsr_event_clients = set()

        # WebUI 导入种子与开启故事不受阻拦，但卡片透出 platform_warning，手动唤醒给出原因
        request.payload = {"bot": "Inwind_NapCat", "payload": SEED}
        async def body():
            return json.dumps(request.payload, ensure_ascii=False).encode("utf-8")
        request.body = body
        await plugin.web_import()

        request.payload = {"bot": "Inwind_NapCat", "action": "on"}
        opened = await plugin.web_story_action()
        assert opened["enabled"] is True
        assert "反向 WebSocket 尚未连接" in opened["platform_warning"]

        request.payload = {"bot": "Inwind_NapCat", "action": "wake"}
        rejected = await plugin.web_story_action()
        assert "反向 WebSocket 尚未连接" in rejected["error"]

        # 无参 ApiNotAvailable() 抛出时，_send_to 日志不再变成空串
        ApiNotAvailable = type("ApiNotAvailable", (Exception,), {})

        async def fail_send(umo, chain):
            raise ApiNotAvailable()

        context.send_message = fail_send
        with caplog.at_level(logging.WARNING):
            ok = await plugin._send_to("Inwind_NapCat:FriendMessage:10001", "在吗")
        assert ok is False
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "Inwind_NapCat:FriendMessage:10001" in messages
        assert "ApiNotAvailable" in messages
        assert "反向 WebSocket 尚未连接" in messages

        # 连上反向 WS 后，状态告警清除
        cq_bot._wsr_api_clients["123456"] = object()
        assert plugin._platform_issue("Inwind_NapCat") == ""
        overview = await plugin.web_story()
        row = next(item for item in overview["bots"] if item["bot_id"] == "Inwind_NapCat")
        assert row["platform_warning"] == ""
    finally:
        await plugin.terminate()
