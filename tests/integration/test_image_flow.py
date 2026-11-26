"""端到端集成测试：聊天拍照→媒体库/发图/空间，以及故事场内拍照与收场自动生成参考图。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.imaging.pipeline import PhotoPipeline
from astrbot_plugin_shadow_memory.imaging.providers import GeneratedImage
from astrbot_plugin_shadow_memory.imaging.refs import RefStore
from astrbot_plugin_shadow_memory.imaging.service import ImageService
from astrbot_plugin_shadow_memory.imaging.temp import PhotoTempStore
from astrbot_plugin_shadow_memory.interaction.media import MediaService
from astrbot_plugin_shadow_memory.interaction.media_dir import MediaDir
from astrbot_plugin_shadow_memory.interaction.qzone import QzoneService
from astrbot_plugin_shadow_memory.story.director import Director
from astrbot_plugin_shadow_memory.story.llm import ROLE_ACTOR, ROLE_GM, StoryLLM
from astrbot_plugin_shadow_memory.story.store import StoryStore

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"X" * 48


class FakeHTTP:
    async def post_json(self, *a, **k): ...
    async def post_multipart(self, *a, **k): ...
    async def get_bytes(self, *a, **k): ...


class FakeGenClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, provider, prompt, refs=None, **kwargs):
        self.prompts.append(prompt)
        return GeneratedImage(PNG_BYTES, "image/png", "png")


class FakeBot:
    def __init__(self) -> None:
        self.actions: list[tuple[str, dict[str, Any]]] = []

    async def call_action(self, action: str, **kwargs: Any):
        self.actions.append((action, kwargs))
        return {"tid": "tid-123"}


class FakeEvent:
    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot
        self.unified_msg_origin = "bot1:FriendMessage:u1"

    def get_platform_name(self) -> str:
        return "aiocqhttp"


async def test_chat_take_photo_integrates_with_media_send_and_qzone(db, workdir, make_configs):
    cfg = make_configs(
        {
            "image": {
                "enabled": True,
                "provider_id": "p1",
                "providers": [
                    {
                        "id": "p1",
                        "protocol": "openai_images",
                        "base_url": "https://img.example/v1",
                        "api_key": "k",
                        "model": "gpt-image-1",
                    }
                ],
                "review_enabled": True,
            }
        }
    )
    audit = Audit(logging.getLogger("test"))
    refs = RefStore(db, cfg, workdir)
    temp = PhotoTempStore(db, cfg, workdir)
    client = FakeGenClient()
    pipeline = PhotoPipeline(
        db=db,
        configs=cfg,
        refs=refs,
        client=client,
        prompt_text=lambda *a: _ret('{"prompt":"desk lamp photo","ref_ids":[]}'),
        review_vision=lambda *a: _ret({"pass": True, "reason": "ok", "fix_hint": "", "description": "台灯"}),
        audit=audit,
    )
    img_svc = ImageService(
        db=db, configs=cfg, refs=refs, temp=temp, client=client, pipeline=pipeline, audit=audit
    )
    dir_provider = lambda b: MediaDir(workdir / "qzone_media", b)
    media = MediaService(
        db, cfg, dir_provider, FakeHTTP(), audit, photo_resolver=temp.read
    )
    qzone = QzoneService(db, cfg, dir_provider, audit, photo_resolver=temp.read)

    photo = await img_svc.take_photo(bot_id="bot1", scene="拍一下台灯")
    assert photo["ok"] is True
    handle = photo["handle"]

    # 1. send_image 解析 photo:<id>
    resolved = await media.resolve_for_send(None, bot_id="bot1", source=handle)
    assert resolved["data"] == PNG_BYTES

    # 2. media(save) 存入正式图片库
    bot = FakeBot()
    event = FakeEvent(bot)
    saved = await media.save(event, bot_id="bot1", source=handle, name="lamp", description="书桌台灯")
    assert saved["ok"] is True
    assert len(await media.list("bot1")) == 1

    # 3. qzone(post) 直接引用 photo:<id> 发空间
    posted = await qzone.post(event, bot_id="bot1", text="晚安", images=[handle])
    assert posted["ok"] is True
    assert bot.actions[0][0] == "send_qzone_msg"
    assert bot.actions[0][1]["images"][0].startswith("base64://")


async def test_story_scene_photo_and_closing_auto_ref(db, workdir, make_configs):
    cfg = make_configs(
        {
            "story": {"enabled": True, "max_rounds": 1, "thinking": "off"},
            "image": {
                "enabled": True,
                "provider_id": "p1",
                "providers": [
                    {
                        "id": "p1",
                        "protocol": "gemini",
                        "base_url": "https://g.example",
                        "api_key": "k",
                        "model": "gemini-2.5-flash-image",
                    }
                ],
                "review_enabled": True,
                "story_max_per_scene": 1,
                "ref": {"auto_generate": True, "require_review": True},
            },
        }
    )
    audit = Audit(logging.getLogger("test"))
    story_store = StoryStore(db)
    await story_store.replace_seed(
        "bot1",
        {
            "version": "world_seed/v1",
            "title": "秋日街角",
            "outline": "她住在安静的老城区，平时在旧书店帮忙整理画册，偶尔去公园散步喂流浪猫。",
            "entries": [],
            "hooks": [],
            "opening": {"where": "旧书店", "doing": "整理书架"},
        },
        now=10_000,
    )
    await db.execute(
        "INSERT INTO sessions(bot_id, umo, name, type, last_message_at, last_summary) "
        "VALUES('bot1', 'bot1:FriendMessage:100', '小明', 'private', 10000, 'hi')"
    )

    refs = RefStore(db, cfg, workdir)
    await refs.add_active("bot1", category="style", name="手绘风", data=PNG_BYTES)
    temp = PhotoTempStore(db, cfg, workdir)
    gen_client = FakeGenClient()
    pipeline = PhotoPipeline(
        db=db,
        configs=cfg,
        refs=refs,
        client=gen_client,
        prompt_text=lambda *a: _ret('{"prompt":"orange tabby cat on books","ref_ids":[]}'),
        review_vision=lambda *a: _ret({"pass": True, "reason": "ok", "fix_hint": "", "description": "趴在书堆上的橘猫"}),
        audit=audit,
        story_store=story_store,
        base_card=lambda b: _ret(("温和的书店店员", "test")),
    )
    img_svc = ImageService(
        db=db, configs=cfg, refs=refs, temp=temp, client=gen_client, pipeline=pipeline, audit=audit
    )
    dir_provider = lambda b: MediaDir(workdir / "qzone_media", b)
    media = MediaService(db, cfg, dir_provider, FakeHTTP(), audit, photo_resolver=temp.read)
    qzone = QzoneService(db, cfg, dir_provider, audit, photo_resolver=temp.read)
    fake_bot = FakeBot()

    sent_photos: list[tuple[str, bytes, str]] = []
    bg_tasks: list[asyncio.Task] = []

    async def fake_send_photo(umo: str, data: bytes, caption: str) -> bool:
        sent_photos.append((umo, data, caption))
        return True

    async def fake_post_qzone(*, bot_id: str, text: str, images: list[str] | None = None):
        return await qzone.post_via_bot(fake_bot, bot_id=bot_id, text=text, images=images)

    def spawn_bg(coro):
        bg_tasks.append(asyncio.create_task(coro))

    # 构造最小假 StoryLLM（开场 -> 行动带 photo -> 裁定结束 -> 收场带 ref_requests）
    gm_replies = iter(
        [
            {"narration": "午后阳光洒进旧书店，一只橘猫跳上柜台打盹。", "lookup": []},
            {"narration": "橘猫眯起眼睛，尾巴轻轻晃了一下。", "lookup": [], "settled": [], "end": True},
            {
                "epilogue": "窗外的风轻轻吹过。",
                "card": {"where": "旧书店", "doing": "陪橘猫晒太阳", "minutes": 60, "phone": "方便"},
                "next_wake_minutes": 60,
                "canon": [{"category": "item", "name": "旧书店橘猫", "body": "胖乎乎的短毛橘猫，右耳有个小缺口。"}],
                "tree": {"threads": []},
                "summary": "她在旧书店偶遇一只橘猫并拍了照。",
                "remote": [],
                "ref_requests": [
                    {"category": "item", "name": "旧书店橘猫", "appearance": "胖乎乎的短毛橘猫，右耳有个小缺口"}
                ],
            },
        ]
    )
    actor_replies = iter(
        [
            {
                "inner": "好可爱，拍张照留念顺便发给小明！",
                "action": "我举起手机悄悄拍了张橘猫打盹的照片。",
                "messages": [],
                "photo": {
                    "scene": "阳光下趴在旧书店柜台上的橘猫",
                    "to": "小明",
                    "text": "你看店里新来的小家伙",
                    "save_media": True,
                    "qzone": True,
                    "qzone_text": "今日份书店摸鱼猫",
                },
            }
        ]
    )

    class _Provider:
        def __init__(self, role: str) -> None:
            self.role = role

        async def text_chat(self, **kwargs):
            data = next(gm_replies if self.role == ROLE_GM else actor_replies)
            return type("Resp", (), {"completion_text": json.dumps(data, ensure_ascii=False)})()

    async def resolver(role: str, bot_id: str):
        return _Provider(role)

    class _MemoryStub:
        def __init__(self) -> None:
            self.dynamics = type("D", (), {"s0_event": lambda self, s: 7.0})()
            self.textgen = type(
                "T",
                (),
                {
                    "generate_json": lambda self, *a, **k: _ret(
                        {"fragment": "拍了橘猫照片", "summary": "在书店拍橘猫", "entities": ["旧书店橘猫"], "salience": 3}
                    )
                },
            )()
            self.edges = type("E", (), {"link": lambda self, *a, **k: _ret(None)})()
            self.ambient = type("A", (), {"on_event_closed": lambda self, *a, **k: _ret(None)})()
            self.jev = None

        async def prepare_turn(self, *a, **k):
            return type("Prep", (), {"persona_text": "", "ambient_text": "", "parts": [], "injection": None})()

    director = Director(
        db=db,
        configs=cfg,
        store=story_store,
        memory=_MemoryStub(),
        llm=StoryLLM(resolver, audit),
        audit=audit,
        base_card=lambda b: _ret(("温和的书店店员", "test")),
        now_fn=lambda: 20_000,
        image_service=img_svc,
        send_photo=fake_send_photo,
        save_photo_media=media.save_raw,
        post_qzone_photo=fake_post_qzone,
        spawn_bg=spawn_bg,
    )

    result = await director.run("bot1", manual=True)
    assert result.scene_id > 0
    if bg_tasks:
        await asyncio.gather(*bg_tasks)

    # 验证场内拍照三个去向均生效：发会话 + 存媒体库 + 发空间
    assert len(sent_photos) == 1
    assert sent_photos[0][0] == "bot1:FriendMessage:100"
    assert sent_photos[0][1] == PNG_BYTES
    assert len(await media.list("bot1")) == 1
    assert len(fake_bot.actions) == 1 and fake_bot.actions[0][0] == "send_qzone_msg"

    # 验证收场异步自动生成参考图已进入审核区
    pending = await refs.pending_summary("bot1")
    assert pending["total"] == 1
    assert pending["groups"][0]["name"] == "旧书店橘猫"


async def _ret(val):
    return val
