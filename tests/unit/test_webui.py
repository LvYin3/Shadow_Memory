from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_shadow_memory.core.kv import KvStore
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
    assert set(data) >= {"persona", "facts", "events", "insights", "notes", "media"}
    assert len(data["persona"]) <= 20


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
