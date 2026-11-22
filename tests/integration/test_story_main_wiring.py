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
        self.persona = dict(BUILTIN_PERSONA)

        async def list_jobs(*args, **kwargs):
            return []

        async def add_basic_job(**kwargs):
            return SimpleNamespace(job_id=f"job-{kwargs.get('name')}")

        async def delete_job(job_id):
            return None

        async def default_persona(umo=None):
            return self.persona

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
        return self.default_provider

    def get_using_provider(self, umo=""):
        return self.default_provider

    def get_config(self, umo=None):
        return {}

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
    module("astrbot.api.event", AstrMessageEvent=object, filter=_Filter())
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
        error_response=lambda message, status=400: {"error": message, "status": status},
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
