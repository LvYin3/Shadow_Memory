"""生图 WebUI 编排、提供商密钥脱敏写回与参考图跨机器人迁移单测。"""

from __future__ import annotations

import base64
import logging
from typing import Any

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.core.kv import KvStore
from astrbot_plugin_shadow_memory.imaging.pipeline import PhotoPipeline
from astrbot_plugin_shadow_memory.imaging.providers import GeneratedImage
from astrbot_plugin_shadow_memory.imaging.ref_transfer import RefTransfer
from astrbot_plugin_shadow_memory.imaging.refs import RefStore
from astrbot_plugin_shadow_memory.imaging.service import ImageService
from astrbot_plugin_shadow_memory.imaging.temp import PhotoTempStore
from astrbot_plugin_shadow_memory.interaction.media import MediaService
from astrbot_plugin_shadow_memory.interaction.media_dir import MediaDir
from astrbot_plugin_shadow_memory.interaction.media_transfer import MediaTransfer
from astrbot_plugin_shadow_memory.memory.ambient import AmbientService
from astrbot_plugin_shadow_memory.memory.dynamics import Dynamics
from astrbot_plugin_shadow_memory.memory.edges import EdgeRepo
from astrbot_plugin_shadow_memory.memory.persona import PersonaService
from astrbot_plugin_shadow_memory.memory.transfer import TransferService
from astrbot_plugin_shadow_memory.webui.service import WebUIService

PNG_DATA = b"\x89PNG\r\n\x1a\n" + b"Z" * 40


class DummyHTTP:
    async def post_json(self, *a, **k): ...
    async def post_multipart(self, *a, **k): ...
    async def get_bytes(self, *a, **k): ...


class DummyClient:
    async def generate(self, provider, prompt, refs=None, **kwargs):
        return GeneratedImage(PNG_DATA, "image/png", "png")


async def _build_webui(db, workdir, raw_cfg: dict[str, Any], saved_flag: list[bool]):
    def configs(bot_id: str = ""):
        from astrbot_plugin_shadow_memory.core.config import ConfigView

        return ConfigView(raw_cfg, bot_id)

    audit = Audit(logging.getLogger("test"))
    refs = RefStore(db, configs, workdir)
    temp = PhotoTempStore(db, configs, workdir)
    client = DummyClient()
    pipeline = PhotoPipeline(
        db=db,
        configs=configs,
        refs=refs,
        client=client,
        prompt_text=lambda *a: _ret('{"prompt":"clean ref"}'),
        review_vision=lambda *a: _ret({"pass": True, "reason": "ok", "fix_hint": "", "description": "ok"}),
        audit=audit,
    )
    img_svc = ImageService(
        db=db, configs=configs, refs=refs, temp=temp, client=client, pipeline=pipeline, audit=audit
    )
    media = MediaService(
        db, configs, lambda b: MediaDir(workdir / "qzone_media", b), DummyHTTP(), audit
    )
    persona = PersonaService(db, configs, audit)
    edges = EdgeRepo(db)
    transfer = TransferService(
        db,
        configs,
        persona,
        edges,
        MediaTransfer(db, media, workdir),
        audit,
        ref_transfer=RefTransfer(db, refs, workdir),
    )
    ambient = AmbientService(db, configs, None, audit)
    webui = WebUIService(
        db,
        configs,
        None,
        ambient,
        media,
        transfer,
        KvStore(db),
        workdir,
        audit,
        tts_providers=lambda: [],
        platform_ids=lambda: ["bot1", "bot2"],
        image=img_svc,
        config_raw=raw_cfg,
        save_config=lambda: saved_flag.append(True),
    )
    # Minimal stub for memory.bot_ids()
    class _Mem:
        async def bot_ids(self):
            return ["bot1", "bot2"]

    webui.memory = _Mem()
    return webui, refs, transfer


async def test_provider_keys_masked_and_preserved_in_plugin_config_only(db, workdir):
    raw_cfg: dict[str, Any] = {}
    saved: list[bool] = []
    webui, _, _ = await _build_webui(db, workdir, raw_cfg, saved)

    res = await webui.image_providers_save(
        {
            "enabled": True,
            "default_provider_id": "oa1",
            "providers": [
                {
                    "id": "oa1",
                    "name": "OpenAI",
                    "protocol": "openai_images",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-secret-value",
                    "model": "gpt-image-1",
                }
            ],
        }
    )
    assert res["ok"] is True
    assert saved == [True]
    assert res["providers"][0]["api_key_set"] is True
    assert "api_key" not in res["providers"][0]
    assert raw_cfg["image"]["providers"][0]["api_key"] == "sk-secret-value"

    # 留空再次保存时保留旧密钥，且密钥绝不写入 memory.db
    res2 = await webui.image_providers_save(
        {
            "enabled": True,
            "default_provider_id": "oa1",
            "providers": [
                {
                    "id": "oa1",
                    "name": "OpenAI Renamed",
                    "protocol": "openai_images",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "",
                    "model": "gpt-image-1",
                }
            ],
        }
    )
    assert res2["providers"][0]["name"] == "OpenAI Renamed"
    assert raw_cfg["image"]["providers"][0]["api_key"] == "sk-secret-value"
    kv_rows = await db.fetchall("SELECT * FROM kv")
    assert "sk-secret-value" not in str(kv_rows)


async def test_webui_ref_upload_pending_and_cross_bot_transfer(db, workdir):
    raw_cfg: dict[str, Any] = {
        "image": {
            "enabled": True,
            "provider_id": "p1",
            "providers": [
                {
                    "id": "p1",
                    "name": "P1",
                    "protocol": "gemini",
                    "base_url": "https://g.example",
                    "api_key": "k",
                    "model": "gemini-2.5-flash-image",
                }
            ],
        }
    }
    webui, refs, transfer = await _build_webui(db, workdir, raw_cfg, [])

    b64 = "data:image/png;base64," + base64.b64encode(PNG_DATA).decode("ascii")
    up = await webui.image_ref_upload(
        "bot1",
        {"category": "style", "name": "胶片", "description": "暖色调", "data_base64": b64},
    )
    assert up["ok"] is True

    # 放一条待审图，验证迁移只复制 active 不复制 pending
    await refs.add_pending("bot1", category="item", name="怀表", data=PNG_DATA)
    overview = await webui.image_overview("bot1")
    assert len(overview["active_refs"]) == 1
    assert overview["pending"]["total"] == 1

    preview = await transfer.preview("bot1", "bot2", ["image_refs"], "merge")
    assert preview["image_refs"]["files"] == 1

    done = await transfer.execute("bot1", "bot2", ["image_refs"], "merge")
    assert done["image_refs"]["copied"] == 1
    assert len(await refs.list_active("bot2")) == 1
    assert len(await refs.list_pending("bot2")) == 0


async def test_webui_image_test_photo_returns_data_url_and_metadata(db, workdir):
    raw_cfg: dict[str, Any] = {
        "story": {"enabled": True},
        "image": {
            "enabled": False,
            "provider_id": "p1",
            "providers": [
                {
                    "id": "p1",
                    "name": "GemMain",
                    "protocol": "gemini",
                    "base_url": "https://g.example",
                    "api_key": "k",
                    "model": "gemini-2.5-flash-image",
                }
            ],
        },
    }
    webui, refs, _ = await _build_webui(db, workdir, raw_cfg, [])
    await refs.add_active("bot1", category="style", name="手绘", data=PNG_DATA)

    overview = await webui.image_overview("bot1")
    assert overview["story_enabled"] is True

    res = await webui.image_test_photo(
        "bot1",
        {
            "scene": "窗台上的多肉植物",
            "provider_id": "p1",
            "include_story": False,
            "recent_chat": "小明：花养得怎么样",
            "run_review": False,
        },
    )
    assert res["ok"] is True
    assert res["data_url"].startswith("data:image/png;base64,")
    assert res["filename"].startswith("bot1-test-") and res["filename"].endswith(".png")
    assert res["provider_id"] == "p1"
    assert res["review"] is None
    assert "translate_meta" in res and res["translate_meta"]["mode"] == "style_and_self"
    assert len(res["refs"]) == 1 and res["refs"][0]["name"] == "手绘"
    assert "花养得怎么样" in res["context_text"]


async def _ret(val):
    return val
