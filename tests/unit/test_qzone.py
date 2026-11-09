from __future__ import annotations

import pytest

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.interaction.qzone import QzoneService
from astrbot_plugin_shadow_memory.interaction.media_dir import MediaDir, MediaError

from ..fakes import FakeEvent, FakeImage

NOW = 1_800_000_000_000


@pytest.fixture
def media(workdir):
    root = workdir / "media"
    root.mkdir()
    instance = MediaDir(root, "bot1", max_images=2, max_image_mb=1, allow_event_images=True)
    instance.ensure_dir()
    return instance


@pytest.fixture
def qzone(db, make_configs, media):
    cfg = make_configs({"interaction": {"qzone": {"daily_limit": 2, "ugc_right": 1}}})
    return QzoneService(db, cfg, lambda bot_id: media, Audit(), now_fn=lambda: NOW)


# --------------------------------------------------------------------- 图片解析
async def test_url_passthrough(media):
    assert await media.resolve(["https://example.com/a.jpg"]) == ["https://example.com/a.jpg"]


async def test_local_filename_becomes_file_uri(media):
    (media.media_dir / "a.png").write_bytes(b"xx")
    refs = await media.resolve(["a.png"])
    assert refs[0].startswith("file://")


async def test_path_traversal_rejected(media):
    with pytest.raises(MediaError):
        await media.resolve(["../secret.jpg"])
    with pytest.raises(MediaError):
        await media.resolve(["..\\secret.jpg"])
    with pytest.raises(MediaError):
        await media.resolve(["sub/a.jpg"])


async def test_bad_extension_rejected(media):
    (media.media_dir / "a.txt").write_bytes(b"xx")
    with pytest.raises(MediaError):
        await media.resolve(["a.txt"])


async def test_missing_file_rejected(media):
    with pytest.raises(MediaError):
        await media.resolve(["nope.jpg"])


async def test_too_many_images_rejected(media):
    (media.media_dir / "a.png").write_bytes(b"x")
    (media.media_dir / "b.png").write_bytes(b"x")
    (media.media_dir / "c.png").write_bytes(b"x")
    with pytest.raises(MediaError):
        await media.resolve(["a.png", "b.png", "c.png"])


async def test_oversized_file_rejected(workdir):
    small = MediaDir(workdir / "m2", "bot1", max_image_mb=0.001)
    small.ensure_dir()
    (small.media_dir / "big.png").write_bytes(b"x" * (200 * 1024))
    with pytest.raises(MediaError):
        await small.resolve(["big.png"])


async def test_event_image_to_base64(media):
    event = FakeEvent()
    event.message_obj.message = [FakeImage("QUJD"), FakeImage("REVG")]
    assert await media.resolve(["event:2"], event) == ["base64://REVG"]


async def test_event_image_out_of_range(media):
    event = FakeEvent()
    event.message_obj.message = []
    with pytest.raises(MediaError):
        await media.resolve(["event:1"], event)


async def test_event_images_can_be_disabled(workdir):
    locked = MediaDir(workdir / "m3", "bot1", allow_event_images=False)
    event = FakeEvent()
    event.message_obj.message = [FakeImage("QUJD")]
    with pytest.raises(MediaError):
        await locked.resolve(["event:1"], event)


async def test_list_local_images(media):
    (media.media_dir / "a.png").write_bytes(b"x")
    (media.media_dir / "b.txt").write_bytes(b"x")
    rows = media.list_local()
    assert [row["name"] for row in rows] == ["a.png"]


# --------------------------------------------------------------------- 发布
async def test_post_maps_payload(qzone):
    event = FakeEvent()
    result = await qzone.post(event, bot_id="bot1", text="今天风很好")
    assert result["ok"] is True
    action, payload = event.bot.calls[0]
    assert action == "send_qzone_msg"
    assert payload["content"] == "今天风很好"
    assert "images" not in payload
    assert "ugc_right" not in payload


async def test_post_with_visibility_and_images(qzone, media):
    (media.media_dir / "a.png").write_bytes(b"x")
    event = FakeEvent()
    result = await qzone.post(
        event, bot_id="bot1", text="配一张图", images=["a.png"], ugc_right=4
    )
    assert result["ok"] is True
    _, payload = event.bot.calls[0]
    assert payload["ugc_right"] == 4
    assert payload["images"][0].startswith("file://")


async def test_post_rejects_unknown_visibility(qzone):
    event = FakeEvent()
    result = await qzone.post(event, bot_id="bot1", text="x", ugc_right=16)
    assert result["ok"] is False
    assert "可见范围" in result["message"]
    assert event.bot.calls == []


async def test_post_rejects_unsupported_platform(qzone):
    event = FakeEvent(platform_name="telegram")
    result = await qzone.post(event, bot_id="bot1", text="x")
    assert result["ok"] is False
    assert "发不了" in result["message"]


async def test_post_daily_cap(qzone):
    event = FakeEvent()
    assert (await qzone.post(event, bot_id="bot1", text="一"))["ok"] is True
    assert (await qzone.post(event, bot_id="bot1", text="二"))["ok"] is True
    third = await qzone.post(event, bot_id="bot1", text="三")
    assert third["ok"] is False
    assert "不想再发" in third["message"]


async def test_post_failure_backs_off_for_day(db, make_configs, media):
    cfg = make_configs({"interaction": {"qzone": {"daily_limit": 10}}})
    service = QzoneService(db, cfg, lambda bot_id: media, Audit(), now_fn=lambda: NOW)
    event = FakeEvent()
    event.bot.fail = True
    for _ in range(3):
        result = await service.post(event, bot_id="bot1", text="发不出去")
        assert result["ok"] is False
    blocked = await service.post(event, bot_id="bot1", text="再试")
    assert "改天" in blocked["message"]


async def test_admin_post_bypasses_cap(qzone):
    event = FakeEvent()
    for index in range(4):
        result = await qzone.post(
            event, bot_id="bot1", text=f"第{index}条", bypass_limit=True
        )
        assert result["ok"] is True


async def test_qzone_uses_per_bot_directory(qzone, media, workdir):
    (media.media_dir / "a.png").write_bytes(b"x")
    other = MediaDir(media.root, "bot2")
    other.ensure_dir()
    (other.media_dir / "b.png").write_bytes(b"x")
    event = FakeEvent()
    await qzone.post(event, bot_id="bot1", text="发一张", images=["a.png"])
    assert event.bot.calls[0][1]["images"][0].startswith(media.media_dir.as_uri())
    blocked = await qzone.post(event, bot_id="bot1", text="发另一张", images=["b.png"])
    assert blocked["ok"] is False


async def test_qzone_logs_written(qzone):
    event = FakeEvent()
    await qzone.post(event, bot_id="bot1", text="一条说说")
    rows = await qzone.db.fetchall("SELECT status FROM qzone_logs")
    assert rows[0]["status"] == "ok"
