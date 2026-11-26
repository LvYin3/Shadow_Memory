"""WebUI 的故事页签：概览、开关操作、世界种子导入、故事数据导出、事件来源标记。"""

from __future__ import annotations

import json

import pytest

from astrbot_plugin_shadow_memory.core.kv import KvStore
from astrbot_plugin_shadow_memory.interaction.media import MediaService
from astrbot_plugin_shadow_memory.interaction.media_dir import MediaDir
from astrbot_plugin_shadow_memory.interaction.media_transfer import MediaTransfer
from astrbot_plugin_shadow_memory.memory.transfer import TransferService
from astrbot_plugin_shadow_memory.webui.service import WebUIError, WebUIService

from ..fakes import FakeHTTP
from .story_helpers import BOT, CLOSING, STORY_ON, build_story, script_scene, world_seed


def make_ui(env, configs, workdir, *, with_story: bool = True) -> WebUIService:
    media = MediaService(
        env.db, configs, lambda bot: MediaDir(workdir / "media", bot), FakeHTTP(), env.stack.audit
    )
    transfer = TransferService(
        env.db,
        configs,
        env.stack.persona,
        env.stack.edges,
        MediaTransfer(env.db, media, workdir),
        env.stack.audit,
    )
    return WebUIService(
        env.db,
        configs,
        env.stack.service,
        env.stack.ambient,
        media,
        transfer,
        KvStore(env.db),
        workdir,
        env.stack.audit,
        tts_providers=lambda: [],
        platform_ids=lambda: ["aiocqhttp"],
        story=env.service if with_story else None,
    )


@pytest.fixture
def env(db, make_configs, workdir):
    configs = make_configs(STORY_ON)
    story = build_story(db, configs)
    story.ui = make_ui(story, configs, workdir)
    story.workdir = workdir
    return story


async def test_world_seed_goes_through_the_import_dialog(env):
    result = await env.ui.import_memory(BOT, world_seed())
    assert result["bot_id"] == BOT
    assert result["kind"] == "world_seed"
    assert result["backup"] == ""
    assert result["counts"]["title"] == "青屿"
    assert result["counts"]["entries"] == 2
    assert result["counts"]["hooks"] == 1
    with pytest.raises(WebUIError, match="outline"):
        await env.ui.import_memory(BOT, {"version": "world_seed/v1", "outline": "太短"})


async def test_story_webui_classify_and_toggle_visibility(env):
    await env.ui.import_memory(BOT, world_seed())
    # 默认未分类全部 gm_only
    spoiler = await env.ui.story_spoiler(BOT)
    assert all(row["visibility"] == "gm_only" for row in spoiler["canon"])

    # 手工切换单条可见性
    first_id = spoiler["canon"][0]["id"]
    toggled = await env.ui.story_set_canon_visibility(BOT, first_id, "known")
    assert toggled == {"id": first_id, "visibility": "known"}
    spoiler_after = await env.ui.story_spoiler(BOT)
    by_id = {row["id"]: row["visibility"] for row in spoiler_after["canon"]}
    assert by_id[first_id] == "known"

    # 一键自动提炼底稿与分类
    env.stack.text.push(
        {
            "known_outline": "我在青屿大学读大三，住在学校北门外的合租屋。",
            "entries": [
                {"name": "阿澄", "visibility": "known"},
                {"name": "青屿大学", "visibility": "gm_only"},
            ],
        }
    )
    classified = await env.ui.story_classify(BOT)
    assert classified["known"] == 1 and classified["gm_only"] == 1
    assert "青屿大学" in classified["known_outline"]
    form = (await env.ui.story_spoiler(BOT))["seed"]
    assert form["known_outline"] == classified["known_outline"]
    vis_by_name = {item["name"]: item["visibility"] for item in form["entries"]}
    assert vis_by_name == {"阿澄": "known", "青屿大学": "gm_only"}


async def test_story_overview_lists_every_bot(env):
    await env.ui.import_memory(BOT, world_seed())
    data = await env.ui.story_overview()
    assert data["master_enabled"] is True
    rows = {row["bot_id"]: row for row in data["bots"]}
    assert {BOT, "aiocqhttp"} <= set(rows)  # 只导入过种子的机器人也会列出来
    assert rows[BOT]["has_seed"] and not rows[BOT]["enabled"]
    assert not rows["aiocqhttp"]["has_seed"]
    assert "还没有导入世界种子" in rows["aiocqhttp"]["problems"]


async def test_story_actions(env):
    await env.ui.import_memory(BOT, world_seed())
    with pytest.raises(WebUIError, match="还没开启"):
        await env.ui.story_action(BOT, "wake")
    assert (await env.ui.story_action(BOT, "on"))["enabled"] is True
    assert (await env.ui.story_action(BOT, "pause"))["paused"] is True
    assert (await env.ui.story_action(BOT, "resume"))["paused"] is False
    # wake 只做检查，演出由调用方放到后台
    assert await env.ui.story_action(BOT, "wake") == {"bot_id": BOT, "started": True}
    assert env.llm.calls == []
    assert (await env.ui.story_action(BOT, "off"))["enabled"] is False
    with pytest.raises(WebUIError, match="action 只能是"):
        await env.ui.story_action(BOT, "explode")
    with pytest.raises(WebUIError, match="bot_id"):
        await env.ui.story_action("", "on")
    with pytest.raises(WebUIError, match="世界种子"):
        await env.ui.story_action("aiocqhttp", "on")


async def test_story_export_and_event_source_in_detail(env):
    await env.ui.import_memory(BOT, world_seed())
    await env.ui.story_action(BOT, "on")
    script_scene(env)
    await env.service.tick()

    path, filename = await env.ui.export_memory(BOT, "story")
    assert filename.startswith(f"{BOT}_story_")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == "shadow_story/export/v1"
    assert data["scenes"][0]["summary"] == CLOSING["summary"]

    detail = await env.ui.detail(BOT)
    assert [event["source"] for event in detail["events"]] == ["story"]
    # full 备份带着来源标记，但不含世界设定、笔记和转录
    path, _ = await env.ui.export_memory(BOT, "full")
    full = path.read_text(encoding="utf-8")
    assert json.loads(full)["events"][0]["source"] == "story"
    assert "室友阿澄在瞒着她找工作" not in full
    with pytest.raises(WebUIError, match="full / seed / story"):
        await env.ui.export_memory(BOT, "other")


async def test_story_routes_fail_cleanly_without_the_module(env, make_configs):
    ui = make_ui(env, make_configs(STORY_ON), env.workdir, with_story=False)
    for call in (
        ui.story_overview(),
        ui.story_action(BOT, "on"),
        ui.import_memory(BOT, world_seed()),
        ui.export_memory(BOT, "story"),
    ):
        with pytest.raises(WebUIError, match="故事模块还没准备好"):
            await call


async def test_story_action_refill(env):
    await env.ui.import_memory(BOT, world_seed())
    with pytest.raises(WebUIError, match="还没开启"):
        await env.ui.story_action(BOT, "refill")

    await env.ui.story_action(BOT, "on")
    res = await env.ui.story_action(BOT, "refill")
    assert res["before_tokens"] == 10.0
    assert res["after_tokens"] == 20.0
    assert res["tokens"] == 20.0

    res2 = await env.ui.story_action(BOT, "refill")
    assert res2["before_tokens"] == 20.0
    assert res2["after_tokens"] == 30.0
    assert res2["tokens"] == 30.0

    with pytest.raises(WebUIError, match="唤醒额度已满"):
        await env.ui.story_action(BOT, "refill")
