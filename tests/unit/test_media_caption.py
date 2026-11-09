from __future__ import annotations

import base64

import pytest

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.interaction.media import MediaService
from astrbot_plugin_shadow_memory.interaction.media_dir import MediaDir

from ..fakes import FakeEvent, FakeHTTP, FakeImage

NOW = 1_800_000_000_000
PNG = b"\x89PNG\r\n\x1a\n" + b"png-body" * 8


class FakeCaptioner:
    def __init__(self, value="海边黄昏，风很大", fail=False):
        self.value = value
        self.fail = fail
        self.calls: list[tuple[int, dict]] = []

    async def __call__(self, data, **kwargs):
        self.calls.append((len(data), kwargs))
        if self.fail:
            raise RuntimeError("vision down")
        return self.value


def event_with_image() -> FakeEvent:
    event = FakeEvent()
    event.message_obj.message = [FakeImage(base64.b64encode(PNG).decode("ascii"))]
    return event


def build_service(db, configs, workdir, captioner):
    root = workdir / "library"

    def dir_provider(bot_id: str) -> MediaDir:
        cfg = configs(bot_id)
        return MediaDir(
            root,
            bot_id,
            max_image_mb=cfg.int("interaction.media.max_image_mb", 5),
            max_name_chars=cfg.int("interaction.media.max_name_chars", 32),
        )

    service = MediaService(
        db,
        configs,
        dir_provider,
        FakeHTTP(),
        Audit(),
        now_fn=lambda: NOW,
        url_validator=lambda url: None,
        captioner=captioner,
    )
    service.root = root  # type: ignore[attr-defined]
    return service


async def test_caption_used_when_description_missing(db, configs, workdir):
    captioner = FakeCaptioner()
    service = build_service(db, configs, workdir, captioner)
    result = await service.save(event_with_image(), bot_id="bot1", source="event:1")
    assert result["ok"] is True
    row = await db.fetchone("SELECT description FROM media_items")
    assert row["description"] == "海边黄昏，风很大"
    size, kwargs = captioner.calls[0]
    assert size == len(PNG)
    assert kwargs["mime"] == "image/png"
    assert kwargs["max_chars"] == 80
    assert kwargs["max_tokens"] == 800
    assert kwargs["disable_thinking"] is True
    assert kwargs["prompt"]


async def test_description_given_skips_caption(db, configs, workdir):
    captioner = FakeCaptioner()
    service = build_service(db, configs, workdir, captioner)
    await service.save(
        event_with_image(), bot_id="bot1", source="event:1", description="我说过的印象"
    )
    row = await db.fetchone("SELECT description FROM media_items")
    assert row["description"] == "我说过的印象"
    assert captioner.calls == []


async def test_vision_disabled_falls_back(db, make_configs, workdir):
    configs = make_configs({"vision": {"enabled": False}})
    captioner = FakeCaptioner()
    service = build_service(db, configs, workdir, captioner)
    result = await service.save(event_with_image(), bot_id="bot1", source="event:1")
    assert result["ok"] is True
    row = await db.fetchone("SELECT description FROM media_items")
    assert row["description"] == "对话里的一张图"
    assert captioner.calls == []


async def test_caption_none_falls_back_and_logs(db, configs, workdir):
    captioner = FakeCaptioner(value=None)
    service = build_service(db, configs, workdir, captioner)
    result = await service.save(event_with_image(), bot_id="bot1", source="event:1")
    assert result["ok"] is True
    row = await db.fetchone("SELECT description FROM media_items")
    assert row["description"] == "对话里的一张图"
    logs = await db.fetchall("SELECT status FROM media_logs")
    assert any(log["status"] == "caption_failed" for log in logs)


async def test_caption_failure_does_not_block_save(db, configs, workdir):
    captioner = FakeCaptioner(fail=True)
    service = build_service(db, configs, workdir, captioner)
    result = await service.save(event_with_image(), bot_id="bot1", source="event:1")
    assert result["ok"] is True
    row = await db.fetchone("SELECT description FROM media_items")
    assert row["description"] == "对话里的一张图"


async def test_caption_truncated_to_vision_max_chars(db, make_configs, workdir):
    configs = make_configs({"vision": {"max_chars": 4}})
    captioner = FakeCaptioner(value="一二三四五六七八")
    service = build_service(db, configs, workdir, captioner)
    await service.save(event_with_image(), bot_id="bot1", source="event:1")
    row = await db.fetchone("SELECT description FROM media_items")
    assert row["description"] == "一二三四"


async def test_url_save_gets_caption(db, configs, workdir):
    captioner = FakeCaptioner(value="一张链接来的海边照片")
    service = build_service(db, configs, workdir, captioner)
    service.http.set_get("https://example.com/a.png", PNG)
    result = await service.save(
        event=None, bot_id="bot1", source="https://example.com/a.png"
    )
    assert result["ok"] is True
    row = await db.fetchone("SELECT description, source FROM media_items")
    assert row["description"] == "一张链接来的海边照片"
    assert row["source"] == "url"


@pytest.mark.parametrize("description", ["", "   "])
async def test_blank_description_counts_as_missing(db, configs, workdir, description):
    captioner = FakeCaptioner()
    service = build_service(db, configs, workdir, captioner)
    await service.save(
        event_with_image(), bot_id="bot1", source="event:1", description=description
    )
    assert len(captioner.calls) == 1
