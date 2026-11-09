from __future__ import annotations

import base64
from pathlib import Path

import pytest

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.interaction.media import MediaService
from astrbot_plugin_shadow_memory.interaction.media_dir import (
    MediaDir,
    MediaError,
    sanitize_prefix,
    sniff_image_ext,
)
from astrbot_plugin_shadow_memory.interaction.media_fetch import MediaFetchError, ensure_public_url

from ..fakes import FakeEvent, FakeHTTP, FakeImage

NOW = 1_800_000_000_000
PNG = b"\x89PNG\r\n\x1a\n" + b"png-body" * 8
JPG = b"\xff\xd8\xff\xe0" + b"jpg-body" * 8
GIF = b"GIF89a" + b"gif-body" * 8
WEBP = b"RIFF" + (24).to_bytes(4, "little") + b"WEBP" + b"webp-body" * 8


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def event_with_image(data: bytes = PNG, *, sender: str = "小明") -> FakeEvent:
    event = FakeEvent(sender=sender)
    event.message_obj.message = [FakeImage(b64(data))]
    return event


@pytest.fixture
def media_root(workdir):
    return workdir / "library"


@pytest.fixture
def media_service(db, configs, media_root):
    http = FakeHTTP()

    def dir_provider(bot_id: str) -> MediaDir:
        cfg = configs(bot_id)
        return MediaDir(
            media_root,
            bot_id,
            max_images=cfg.int("interaction.qzone.max_images", 9),
            max_image_mb=cfg.int("interaction.media.max_image_mb", 5),
            allow_event_images=cfg.bool("interaction.media.allow_event_images", True),
            max_name_chars=cfg.int("interaction.media.max_name_chars", 32),
        )

    service = MediaService(
        db,
        configs,
        dir_provider,
        http,
        Audit(),
        now_fn=lambda: NOW,
        url_validator=lambda url: None,
    )
    service.http = http  # type: ignore[attr-defined]
    service.root = media_root  # type: ignore[attr-defined]
    return service


def build_service(db, configs, media_root, http=None, validator=None):
    http = http or FakeHTTP()

    def dir_provider(bot_id: str) -> MediaDir:
        cfg = configs(bot_id)
        return MediaDir(
            media_root,
            bot_id,
            max_image_mb=cfg.int("interaction.media.max_image_mb", 5),
            max_name_chars=cfg.int("interaction.media.max_name_chars", 32),
        )

    return MediaService(
        db,
        configs,
        dir_provider,
        http,
        Audit(),
        now_fn=lambda: NOW,
        url_validator=validator or (lambda url: None),
    )


# --------------------------------------------------------------------- 基础函数
def test_sniff_image_formats():
    assert sniff_image_ext(PNG) == "png"
    assert sniff_image_ext(JPG) == "jpg"
    assert sniff_image_ext(GIF) == "gif"
    assert sniff_image_ext(WEBP) == "webp"
    assert sniff_image_ext(b"not an image") is None
    assert sniff_image_ext(b"") is None


def test_sanitize_prefix_keeps_chinese_and_strips_separators():
    assert sanitize_prefix("海边 的 风") == "海边-的-风"
    assert sanitize_prefix("a/b\\c") == "abc"
    assert sanitize_prefix("!!!") == ""
    assert len(sanitize_prefix("长" * 50, 10)) == 10


def test_media_dir_is_bot_scoped(media_root):
    first = MediaDir(media_root, "bot:1")
    second = MediaDir(media_root, "bot:1")  # 同名稳定
    other = MediaDir(media_root, "bot/2")
    assert first.media_dir == second.media_dir
    assert first.media_dir != other.media_dir
    assert first.media_dir.parent == media_root.resolve()


def test_path_of_rejects_traversal(media_root):
    media = MediaDir(media_root, "bot1")
    media.ensure_dir()
    (media.media_dir / "a.png").write_bytes(PNG)
    with pytest.raises(MediaError):
        media.path_of("../a.png")
    with pytest.raises(MediaError):
        media.path_of("sub/a.png")
    with pytest.raises(MediaError):
        media.path_of("a.txt")


# --------------------------------------------------------------------- 保存
async def test_save_event_image_with_description_and_tags(media_service):
    event = event_with_image()
    result = await media_service.save(
        event,
        bot_id="bot1",
        source="event:1",
        name="海边",
        description="他说这是海边拍的，风很大",
        tags=["海边", "风"],
    )
    assert result["ok"] is True
    assert result["filename"].startswith("海边-")
    assert result["filename"].endswith(".png")
    path = media_service.root / "bot1" / result["filename"]
    assert path.read_bytes() == PNG
    row = await media_service.db.fetchone("SELECT * FROM media_items")
    assert row["description"] == "他说这是海边拍的，风很大"
    assert "海边" in row["tags"]
    assert row["sender_name"] == "小明"


async def test_save_url_image(media_service):
    media_service.http.set_get("https://example.com/a.png", PNG)
    result = await media_service.save(
        event=None, bot_id="bot1", source="https://example.com/a.png", description="链接图"
    )
    assert result["ok"] is True
    assert (media_service.root / "bot1" / result["filename"]).is_file()


async def test_save_rejects_unknown_source(media_service):
    result = await media_service.save(event=None, bot_id="bot1", source="/etc/passwd")
    assert result["ok"] is False
    assert "只能存" in result["message"]


async def test_save_rejects_bad_event_index(media_service):
    event = event_with_image()
    result = await media_service.save(event, bot_id="bot1", source="event:2")
    assert result["ok"] is False
    assert "没有第 2 张图" in result["message"]


async def test_save_rejects_non_image_url(media_service):
    media_service.http.set_get("https://example.com/x", b"just text")
    result = await media_service.save(
        event=None, bot_id="bot1", source="https://example.com/x"
    )
    assert result["ok"] is False
    assert "不是一张" in result["message"]


async def test_save_rejects_failed_download(media_service):
    result = await media_service.save(
        event=None, bot_id="bot1", source="https://example.com/missing.png"
    )
    assert result["ok"] is False
    assert "没取回来" in result["message"]


async def test_save_rejects_oversized_event_image(db, make_configs, media_root):
    configs = make_configs({"interaction": {"media": {"max_image_mb": 1}}})
    service = build_service(db, configs, media_root)
    big = PNG + b"x" * (1024 * 1024 + 10)
    event = event_with_image(big)
    result = await service.save(event, bot_id="bot1", source="event:1")
    assert result["ok"] is False
    assert "太大" in result["message"]
    assert not (media_root / "bot1").exists() or not any((media_root / "bot1").iterdir())


async def test_save_respects_file_quota(db, make_configs, media_root):
    configs = make_configs({"interaction": {"media": {"max_files": 1}}})
    service = build_service(db, configs, media_root)
    first = await service.save(event_with_image(), bot_id="bot1", source="event:1")
    assert first["ok"] is True
    second = await service.save(event_with_image(), bot_id="bot1", source="event:1")
    assert second["ok"] is False
    assert "满了" in second["message"]


async def test_save_respects_total_bytes_quota(db, make_configs, media_root):
    configs = make_configs(
        {"interaction": {"media": {"max_image_mb": 5, "max_total_mb": 1}}}
    )
    service = build_service(db, configs, media_root)
    payload = PNG + b"x" * (600 * 1024)
    first = await service.save(event_with_image(payload), bot_id="bot1", source="event:1")
    assert first["ok"] is True
    second = await service.save(event_with_image(payload), bot_id="bot1", source="event:1")
    assert second["ok"] is False
    assert "满了" in second["message"]


async def test_same_bytes_same_second_get_suffix(media_service):
    first = await media_service.save(event_with_image(), bot_id="bot1", source="event:1")
    second = await media_service.save(event_with_image(), bot_id="bot1", source="event:1")
    assert first["filename"] != second["filename"]
    assert "-2.png" in second["filename"]


async def test_save_falls_back_to_default_description(media_service):
    result = await media_service.save(event_with_image(), bot_id="bot1", source="event:1")
    row = await media_service.db.fetchone("SELECT description FROM media_items")
    assert row["description"] == "对话里的一张图"
    assert result["ok"] is True


async def test_save_write_failure_does_not_write_metadata(db, configs, media_root):
    media_root.mkdir(parents=True, exist_ok=True)
    (media_root / "bot1").write_bytes(b"a file blocks the directory")
    service = build_service(db, configs, media_root)
    result = await service.save(event_with_image(), bot_id="bot1", source="event:1")
    assert result["ok"] is False
    assert "写不进" in result["message"]
    assert await db.scalar("SELECT COUNT(*) FROM media_items") == 0


# --------------------------------------------------------------------- 查询
async def test_list_only_returns_current_bot(media_service):
    await media_service.save(
        event_with_image(), bot_id="bot1", source="event:1", description="bot1 的图"
    )
    assert len(await media_service.list("bot1")) == 1
    assert await media_service.list("bot2") == []


async def test_search_by_description_tag_and_sender(media_service):
    await media_service.save(
        event_with_image(sender="小明"),
        bot_id="bot1",
        source="event:1",
        description="他说这是海边拍的，风很大",
        tags=["海边", "旅行"],
    )
    assert len(await media_service.search("bot1", "海边")) == 1
    assert len(await media_service.search("bot1", "旅行")) == 1
    assert len(await media_service.search("bot1", "小明")) == 1
    assert await media_service.search("bot1", "完全无关的词") == []


async def test_search_empty_query_returns_recent(media_service):
    await media_service.save(
        event_with_image(), bot_id="bot1", source="event:1", description="第一张"
    )
    rows = await media_service.search("bot1", "")
    assert len(rows) == 1


async def test_search_filters_missing_files(media_service):
    result = await media_service.save(
        event_with_image(), bot_id="bot1", source="event:1", description="会被手删掉"
    )
    (media_service.root / "bot1" / result["filename"]).unlink()
    assert await media_service.search("bot1", "手删") == []
    assert await media_service.list("bot1") == []


async def test_stats(media_service):
    await media_service.save(
        event_with_image(), bot_id="bot1", source="event:1", description="有描述"
    )
    stats = await media_service.stats("bot1")
    assert stats["files"] == 1
    assert stats["bytes"] == len(PNG)
    assert stats["described"] == 1


# --------------------------------------------------------------------- 删除
async def test_delete_removes_file_and_row(media_service):
    result = await media_service.save(
        event_with_image(), bot_id="bot1", source="event:1", description="待删"
    )
    outcome = await media_service.delete("bot1", result["filename"])
    assert outcome["ok"] is True
    assert not (media_service.root / "bot1" / result["filename"]).exists()
    assert await media_service.db.scalar("SELECT COUNT(*) FROM media_items") == 0


async def test_delete_missing_file(media_service):
    outcome = await media_service.delete("bot1", "nope.png")
    assert outcome["ok"] is False
    assert "不存在" in outcome["message"]


async def test_delete_rejects_path_traversal(media_service):
    outcome = await media_service.delete("bot1", "../secret.png")
    assert outcome["ok"] is False


async def test_delete_cannot_cross_bots(media_service):
    result = await media_service.save(
        event_with_image(), bot_id="bot1", source="event:1", description="bot1"
    )
    outcome = await media_service.delete("bot2", result["filename"])
    assert outcome["ok"] is False
    assert (media_service.root / "bot1" / result["filename"]).exists()


# --------------------------------------------------------------------- 审计
async def test_media_logs_written(media_service):
    result = await media_service.save(
        event_with_image(), bot_id="bot1", source="event:1", description="日志"
    )
    await media_service.delete("bot1", result["filename"])
    rows = await media_service.db.fetchall("SELECT action, status FROM media_logs ORDER BY id")
    assert [row["action"] for row in rows] == ["save", "delete"]
    assert all(row["status"] == "ok" for row in rows)


# --------------------------------------------------------------------- URL 安全
def test_ensure_public_url_rejects_private_and_bad_scheme():
    with pytest.raises(MediaFetchError):
        ensure_public_url("http://127.0.0.1/a.png")
    with pytest.raises(MediaFetchError):
        ensure_public_url("http://10.0.0.5/a.png")
    with pytest.raises(MediaFetchError):
        ensure_public_url("ftp://example.com/a.png")
    with pytest.raises(MediaFetchError):
        ensure_public_url("https://example.com/a.png", resolver=lambda host: ["192.168.1.2"])
    ensure_public_url("https://example.com/a.png", resolver=lambda host: ["93.184.216.34"])


async def test_save_rejects_private_url(db, configs, media_root):
    from astrbot_plugin_shadow_memory.interaction.media_fetch import ensure_public_url

    service = build_service(db, configs, media_root, validator=ensure_public_url)
    result = await service.save(
        event=None, bot_id="bot1", source="http://127.0.0.1/a.png"
    )
    assert result["ok"] is False
    assert "内网" in result["message"]


# --------------------------------------------------------------------- 发图取数
async def test_resolve_for_send_from_event(media_service):
    resolved = await media_service.resolve_for_send(
        event_with_image(), bot_id="bot1", source="event:1"
    )
    assert resolved["data"] == PNG
    assert resolved["filename"] is None


async def test_resolve_for_send_from_filename(media_service):
    saved = await media_service.save(
        event_with_image(), bot_id="bot1", source="event:1", description="一张图"
    )
    resolved = await media_service.resolve_for_send(
        event=None, bot_id="bot1", source=saved["filename"]
    )
    assert resolved["data"] == PNG
    assert resolved["filename"] == saved["filename"]


async def test_resolve_for_send_from_url(media_service):
    media_service.http.set_get("https://example.com/b.png", PNG)
    resolved = await media_service.resolve_for_send(
        event=None, bot_id="bot1", source="https://example.com/b.png"
    )
    assert resolved["data"] == PNG


async def test_resolve_for_send_rejects_missing_file(media_service):
    with pytest.raises(MediaError):
        await media_service.resolve_for_send(event=None, bot_id="bot1", source="nope.png")


async def test_resolve_for_send_rejects_empty_source(media_service):
    with pytest.raises(MediaError):
        await media_service.resolve_for_send(event=None, bot_id="bot1", source="")
