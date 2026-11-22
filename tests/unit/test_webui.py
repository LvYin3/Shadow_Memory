from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_shadow_memory.core.config import DEFAULT_GATE_QUESTION
from astrbot_plugin_shadow_memory.core.kv import KvStore
from astrbot_plugin_shadow_memory.core.types import Turn
from astrbot_plugin_shadow_memory.interaction.media import MediaService
from astrbot_plugin_shadow_memory.interaction.media_dir import MediaDir
from astrbot_plugin_shadow_memory.interaction.media_transfer import MediaTransfer
from astrbot_plugin_shadow_memory.memory.transfer import TransferService
from astrbot_plugin_shadow_memory.webui.service import WebUIError, WebUIService

from ..fakes import FakeHTTP
from .helpers import build_stack, text_of_len


def seed_payload():
    return {
        "version": "persona_seed/v1",
        "bot_hint": "bot1",
        "persona": {"core": [text_of_len(60, "我骨子里")], "edge": [], "tension": []},
        "facts": [
            {
                "kind": "world",
                "subject_id": "u1",
                "text": "小明住在杭州",
                "key_fact_type": "none",
                "salience": 3,
            }
        ],
        "events": [
            {
                "fragment": "第一次见面是在雨天",
                "summary": "",
                "entities": ["小明", "雨"],
                "occurred_days_ago": 3,
                "salience": 3,
                "participants": [],
            }
        ],
    }


@pytest.fixture
def env(db, make_configs, workdir):
    configs = make_configs(
        {"interaction": {"tts_provider_ids": {"bot1": "tts-config"}}}
    )
    stack = build_stack(db, configs)
    root = workdir / "media"
    media = MediaService(
        db, configs, lambda bot: MediaDir(root, bot), FakeHTTP(), stack.audit
    )
    transfer = TransferService(
        db,
        configs,
        stack.persona,
        stack.edges,
        MediaTransfer(db, media, workdir),
        stack.audit,
    )
    kv = KvStore(db)
    tts = [{"id": "tts-config", "label": "tts-config", "model": "m"}]
    ui = WebUIService(
        db,
        configs,
        stack.service,
        stack.ambient,
        media,
        transfer,
        kv,
        workdir,
        stack.audit,
        tts_providers=lambda: tts,
        platform_ids=lambda: ["aiocqhttp"],
    )
    return SimpleNamespace(
        stack=stack, configs=configs, ui=ui, kv=kv, workdir=workdir, tts=tts
    )


async def test_overview_includes_data_and_kv_bots(env):
    await env.stack.seed.import_seed("bot1", seed_payload(), force=True)
    await env.kv.set("bot2", "tts_provider_id", "tts-config")
    data = await env.ui.overview()
    ids = {row["bot_id"] for row in data["bots"]}
    assert {"bot1", "bot2"} <= ids
    bot1 = next(row for row in data["bots"] if row["bot_id"] == "bot1")
    assert bot1["persona"]["counts"]["core"] == 1
    assert data["totals"]["facts"] == 1
    platform = next(row for row in data["bots"] if row["bot_id"] == "aiocqhttp")
    assert platform["persona"]["counts"]["core"] == 0
    assert platform["facts"]["active"] == 0


async def test_detail_limits(env):
    await env.stack.seed.import_seed("bot1", seed_payload(), force=True)
    data = await env.ui.detail("bot1")
    assert set(data) >= {"ambient", "persona", "facts", "events", "insights", "notes", "media"}
    assert set(data["ambient"]) >= {"text", "recent", "people", "important"}
    assert len(data["persona"]) <= 20


async def test_detail_events_expose_rebuild_metadata_and_rebuild(env):
    await env.stack.service.observe(
        Turn("bot1", "umo1", "user", "我爸妈支持我喜欢的东西", 1_700_000_000_000, "u1", "小明"),
        display_name="测试群",
        chat_type="group",
    )
    env.stack.text.push({"fragment": "我爸妈支持我喜欢的东西", "entities": ["爸妈"]})
    await env.stack.segmenter.close_event(1)
    data = await env.ui.detail("bot1")
    event = data["events"][0]
    assert event["id"] == 1
    assert event["has_turns"] == 1
    assert event["summary"] == "" or event["summary"] is None

    env.stack.text.push({"fragment": "小明说爸妈支持他喜欢的东西", "entities": ["小明"]})
    rebuilt = await env.ui.rebuild_event("bot1", 1)
    assert rebuilt["fragment"] == "小明说爸妈支持他喜欢的东西"
    with pytest.raises(WebUIError):
        await env.ui.rebuild_event("bot2", 1)


async def test_rebuild_event_rejects_event_without_turns(env):
    await env.stack.db.execute(
        "INSERT INTO events(id, bot_id, umo, kind, started_at, fragment, status, created_at) "
        "VALUES(9,'bot1','umo1','episodic',1,'旧记忆','closed',1)"
    )
    with pytest.raises(WebUIError, match="没有原始对话"):
        await env.ui.rebuild_event("bot1", 9)


async def test_settings_save_and_clear(env):
    await env.stack.seed.import_seed("bot1", seed_payload(), force=True)
    data = await env.ui.settings()
    assert data["tts_providers"][0]["id"] == "tts-config"
    bot1 = next(row for row in data["bots"] if row["bot_id"] == "bot1")
    assert bot1["tts_source"] == "config"

    await env.ui.save_settings("bot1", tts_provider_id="tts-config", context_recent_messages=8)
    data = await env.ui.settings()
    bot1 = next(row for row in data["bots"] if row["bot_id"] == "bot1")
    assert bot1["tts_source"] == "db"
    assert bot1["context_recent_messages"] == "8"

    await env.ui.save_settings("bot1", tts_provider_id="")
    data = await env.ui.settings()
    bot1 = next(row for row in data["bots"] if row["bot_id"] == "bot1")
    assert bot1["tts_source"] == "muted"

    await env.ui.save_settings("bot1", clear_tts=True, clear_context=True)
    data = await env.ui.settings()
    bot1 = next(row for row in data["bots"] if row["bot_id"] == "bot1")
    assert bot1["tts_source"] == "config"
    assert bot1["context_recent_messages"] is None


async def test_settings_validate_provider_and_context(env):
    with pytest.raises(WebUIError):
        await env.ui.save_settings("bot1", tts_provider_id="nope")
    with pytest.raises(WebUIError):
        await env.ui.save_settings("bot1", context_recent_messages=99)


def _bot(data, bot_id="bot1"):
    return next(row for row in data["bots"] if row["bot_id"] == bot_id)


async def test_settings_gate_question_defaults_to_builtin(env):
    bot1 = _bot(await env.ui.settings())
    assert bot1["gate_question"] == DEFAULT_GATE_QUESTION
    assert bot1["gate_question_source"] == "builtin"


async def test_settings_gate_question_save_and_clear(env):
    saved = await env.ui.save_settings("bot1", gate_question="  Should I reply?\r\n")
    assert saved["gate_question"] == "Should I reply?"
    assert await env.kv.get("bot1", "gate_question") == "Should I reply?"
    bot1 = _bot(await env.ui.settings())
    assert bot1["gate_question"] == "Should I reply?"
    assert bot1["gate_question_source"] == "webui"

    # 清空 = 跟随配置
    saved = await env.ui.save_settings("bot1", gate_question="   ")
    assert saved["gate_question"] is None
    assert await env.kv.get("bot1", "gate_question") is None
    bot1 = _bot(await env.ui.settings())
    assert bot1["gate_question"] == DEFAULT_GATE_QUESTION
    assert bot1["gate_question_source"] == "builtin"


async def test_settings_gate_question_same_as_config_writes_no_override(env):
    # 文本框预填当前生效的问题；原样提交回来不能变成覆盖
    await env.ui.save_settings("bot1", gate_question=DEFAULT_GATE_QUESTION)
    assert await env.kv.get("bot1", "gate_question") is None

    # 已有覆盖时改回配置里的原文，覆盖被删除
    await env.ui.save_settings("bot1", gate_question="Should I reply?")
    await env.ui.save_settings("bot1", gate_question=DEFAULT_GATE_QUESTION)
    assert await env.kv.get("bot1", "gate_question") is None


async def test_settings_gate_question_follows_config_layers(env, make_configs):
    configs = make_configs(
        {
            "interaction": {"gate_question": "global question"},
            "bot_overrides": {"bot1": {"gate_question": "bot question"}},
        }
    )
    env.ui.configs = configs
    data = await env.ui.settings()
    assert _bot(data)["gate_question"] == "bot question"
    assert _bot(data)["gate_question_source"] == "bot_override"

    await env.ui.save_settings("bot1", gate_question="bot question")
    assert await env.kv.get("bot1", "gate_question") is None

    await env.ui.save_settings("bot1", gate_question="global question")
    assert await env.kv.get("bot1", "gate_question") == "global question"
    assert _bot(await env.ui.settings())["gate_question_source"] == "webui"


async def test_settings_gate_question_untouched_by_other_saves(env):
    await env.ui.save_settings("bot1", gate_question="Should I reply?")
    await env.ui.save_settings("bot1", tts_provider_id="tts-config", context_recent_messages=8)
    assert await env.kv.get("bot1", "gate_question") == "Should I reply?"

    await env.ui.save_settings("bot1", clear_tts=True, clear_context=True)
    assert await env.kv.get("bot1", "gate_question") == "Should I reply?"


async def test_settings_gate_question_validation(env):
    with pytest.raises(WebUIError):
        await env.ui.save_settings("bot1", gate_question="x" * 2001)
    with pytest.raises(WebUIError):
        await env.ui.save_settings("bot1", gate_question=123)
    assert await env.kv.get("bot1", "gate_question") is None

    # 门控问题不合法时，同一次保存里的其它项也不写
    with pytest.raises(WebUIError):
        await env.ui.save_settings(
            "bot1", context_recent_messages=8, gate_question="x" * 2001
        )
    assert await env.kv.get("bot1", "context_recent_messages") is None

    await env.ui.save_settings("bot1", gate_question="x" * 2000)
    assert await env.kv.get("bot1", "gate_question") == "x" * 2000


async def test_settings_gate_question_long_config_value_round_trips(env, make_configs):
    # 配置层的问题不受 WebUI 长度限制；原样提交回来不能报错
    long_question = "y" * 3000
    env.ui.configs = make_configs(
        {
            "interaction": {
                "gate_question": long_question,
                "tts_provider_ids": {"bot1": "tts-config"},
            }
        }
    )
    assert _bot(await env.ui.settings())["gate_question"] == long_question
    await env.ui.save_settings("bot1", gate_question=long_question)
    assert await env.kv.get("bot1", "gate_question") is None


async def test_export_full_and_import(env):
    await env.stack.seed.import_seed("bot1", seed_payload(), force=True)
    path, filename = await env.ui.export_memory("bot1", "full")
    assert path.is_file()
    assert filename.endswith(".json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == "shadow_memory/full/v2"

    result = await env.ui.import_memory("bot2", payload, force=True)
    assert result["counts"]["persona"] == 1
    assert result["counts"]["events"] == 1


async def test_import_seed_and_unknown_version(env):
    result = await env.ui.import_memory("bot3", seed_payload())
    assert result["counts"]["facts"] == 1
    with pytest.raises(WebUIError, match="版本"):
        await env.ui.import_memory("bot4", {"version": "nope"})


async def test_bad_bot_id(env):
    with pytest.raises(WebUIError):
        await env.ui.detail("../x")
