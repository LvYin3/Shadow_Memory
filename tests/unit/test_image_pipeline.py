"""生图核心流水线单测：故事上下文注入（含主持人笔记）、视觉审核重试、fail-closed 与配额。"""

from __future__ import annotations

import json
import logging
from typing import Any

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.imaging.pipeline import PhotoPipeline
from astrbot_plugin_shadow_memory.imaging.providers import GeneratedImage
from astrbot_plugin_shadow_memory.imaging.refs import RefStore
from astrbot_plugin_shadow_memory.imaging.service import ImageService
from astrbot_plugin_shadow_memory.imaging.temp import PhotoTempStore
from astrbot_plugin_shadow_memory.story.store import StoryStore

PNG_1 = b"\x89PNG\r\n\x1a\n" + b"1" * 32
PNG_2 = b"\x89PNG\r\n\x1a\n" + b"2" * 32


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.outputs = [
            GeneratedImage(PNG_1, "image/png", "png"),
            GeneratedImage(PNG_2, "image/png", "png"),
        ]

    async def generate(self, provider, prompt, refs=None, *, aspect_ratio="", size="", max_bytes=8_000_000):
        self.calls.append(
            {"provider": provider, "prompt": prompt, "refs": list(refs or []), "aspect_ratio": aspect_ratio}
        )
        idx = min(len(self.calls) - 1, len(self.outputs) - 1)
        return self.outputs[idx]


def _provider_cfg() -> dict[str, Any]:
    return {
        "id": "p1",
        "name": "TestGen",
        "protocol": "openai_images",
        "base_url": "https://img.example/v1",
        "api_key": "sk-1",
        "model": "gpt-image-1",
    }


async def test_pipeline_injects_full_story_context_and_retries_on_reject(
    db, workdir, make_configs
):
    cfg = make_configs(
        {
            "story": {"enabled": True},
            "image": {
                "enabled": True,
                "provider_id": "p1",
                "providers": [_provider_cfg()],
                "review_enabled": True,
                "max_retries": 1,
                "return_description": False,
                "daily_limit": 5,
            },
        }
    )
    audit = Audit(logging.getLogger("test"))
    story_store = StoryStore(db)
    now = 1_750_000_000_000
    await story_store.replace_seed(
        "bot1",
        {
            "version": "world_seed/v1",
            "title": "海边小镇",
            "outline": "她住在海风吹过的老街二楼，日常在街角咖啡馆画画与写信，空气里总有海盐和烘焙豆的味道。",
            "entries": [
                {"category": "place", "name": "灯塔咖啡馆", "body": "落地窗朝海，木吧台挂着铜铃。"}
            ],
            "hooks": [],
            "opening": {"where": "灯塔咖啡馆", "doing": "喝手冲"},
        },
        now=now - 10_000,
    )
    await story_store.update_state(
        "bot1",
        now=now - 10_000,
        card=json.dumps(
            {"where": "灯塔咖啡馆", "doing": "靠窗画速写", "until": now + 3_600_000, "phone_ok": True, "updated_at": now - 10_000},
            ensure_ascii=False,
        ),
    )
    await story_store.apply_notes(
        "bot1",
        [{"title": "她的外套", "body": "今天穿了米白色针织开衫，桌面放着蓝皮速写本。"}],
        scene_id=None,
        now=now - 10_000,
    )
    await story_store.insert_scene(
        "bot1",
        cause="manual",
        status="ok",
        started_at=now - 20_000,
        ended_at=now - 10_000,
        transcript=[{"role": "me", "text": "把冰美式放到窗边晒太阳。"}],
        summary="在灯塔咖啡馆靠窗坐下。",
    )

    refs = RefStore(db, cfg, workdir)
    await refs.add_active("bot1", category="style", name="胶片", data=PNG_1)
    cafe = await refs.add_active("bot1", category="place", name="灯塔咖啡馆", data=PNG_2, description="木吧台海景窗")
    temp = PhotoTempStore(db, cfg, workdir)
    client = FakeClient()

    translator_prompts: list[str] = []

    async def fake_translate(bot_id, umo, system, user):
        translator_prompts.append(user)
        return json.dumps(
            {
                "prompt": f"coffee cup by window attempt {len(translator_prompts)}",
                "ref_ids": [cafe["id"]],
                "aspect_ratio": "3:4",
            },
            ensure_ascii=False,
        )

    review_calls = 0

    async def fake_review(bot_id, umo, images, system, user):
        nonlocal review_calls
        review_calls += 1
        if review_calls == 1:
            return {"pass": False, "reason": "杯子变形", "fix_hint": "改为陶瓷马克杯特写", "description": ""}
        return {"pass": True, "reason": "构图自然", "fix_hint": "", "description": "窗边木桌上的手冲咖啡"}

    pipeline = PhotoPipeline(
        db=db,
        configs=cfg,
        refs=refs,
        client=client,
        prompt_text=fake_translate,
        review_vision=fake_review,
        audit=audit,
        story_store=story_store,
        base_card=lambda b: _coro(("温和爱画画的女孩", "test")),
        now_fn=lambda: now,
    )
    svc = ImageService(
        db=db,
        configs=cfg,
        refs=refs,
        temp=temp,
        client=client,
        pipeline=pipeline,
        audit=audit,
    )

    res = await svc.take_photo(bot_id="bot1", scene="拍一张眼前窗边咖啡的照片")
    assert res["ok"] is True
    assert res["handle"].startswith("photo:")
    # 默认不返回成图描述
    assert res["description"] == ""
    assert "画面：" not in res["message"]
    # 验证故事上下文（含大纲、状态卡、上场结尾、设定集、主持人笔记）全部注入给转译模型
    first_prompt = translator_prompts[0]
    assert "老街二楼" in first_prompt
    assert "灯塔咖啡馆" in first_prompt
    assert "冰美式" in first_prompt
    assert "米白色针织开衫" in first_prompt
    # 验证重试时携带审核反馈，且每次都固定带上了画风参考图 + 选中的主体参考图
    assert len(translator_prompts) == 2
    assert "陶瓷马克杯特写" in translator_prompts[1]
    assert len(client.calls) == 2
    assert [r.category for r in client.calls[0]["refs"]] == ["style", "place"]
    assert await temp.read("bot1", res["photo_id"]) == PNG_2


async def test_pipeline_fail_closed_when_reviewer_unavailable_and_quota(
    db, workdir, make_configs
):
    cfg = make_configs(
        {
            "image": {
                "enabled": True,
                "provider_id": "p1",
                "providers": [_provider_cfg()],
                "review_enabled": True,
                "daily_limit": 1,
            }
        }
    )
    audit = Audit(logging.getLogger("test"))
    refs = RefStore(db, cfg, workdir)
    temp = PhotoTempStore(db, cfg, workdir)
    client = FakeClient()

    async def fake_translate(bot_id, umo, system, user):
        return json.dumps({"prompt": "sunny park", "ref_ids": []})

    async def broken_review(bot_id, umo, images, system, user):
        return None  # 模拟审核模型不可用

    pipeline = PhotoPipeline(
        db=db,
        configs=cfg,
        refs=refs,
        client=client,
        prompt_text=fake_translate,
        review_vision=broken_review,
        audit=audit,
    )
    svc = ImageService(
        db=db,
        configs=cfg,
        refs=refs,
        temp=temp,
        client=client,
        pipeline=pipeline,
        audit=audit,
    )

    first = await svc.take_photo(bot_id="bot1", scene="公园")
    assert first["ok"] is False
    assert first["code"] == "review_unavailable"

    # daily_limit=1 已耗尽，第二次直接拦下不再调生图 API
    second = await svc.take_photo(bot_id="bot1", scene="公园")
    assert second["ok"] is False
    assert second["code"] == "quota"
    assert len(client.calls) == 1


async def test_webui_test_photo_bypasses_switch_quota_and_optional_review(
    db, workdir, make_configs
):
    cfg = make_configs(
        {
            "story": {"enabled": True},
            "image": {
                "enabled": False,  # 总开关关闭也能测
                "provider_id": "p1",
                "providers": [
                    _provider_cfg(),
                    {
                        "id": "p2",
                        "name": "AltGemini",
                        "protocol": "gemini",
                        "base_url": "https://gem.example",
                        "api_key": "k2",
                        "model": "gemini-2.5-flash-image",
                    },
                ],
                "daily_limit": 1,
            },
        }
    )
    audit = Audit(logging.getLogger("test"))
    story_store = StoryStore(db)
    now = 1_750_000_000_000
    await story_store.replace_seed(
        "bot1",
        {
            "version": "world_seed/v1",
            "title": "雪夜车站",
            "outline": "她生活在常年下雪的北方海港小镇，喜欢在站台小亭喝热可可并记录过往列车。",
            "entries": [],
            "hooks": [],
            "opening": {"where": "站台小亭", "doing": "喝热可可"},
        },
        now=now,
    )
    refs = RefStore(db, cfg, workdir)
    await refs.add_active("bot1", category="style", name="油画风", data=PNG_1)
    temp = PhotoTempStore(db, cfg, workdir)
    client = FakeClient()

    seen_contexts: list[str] = []
    review_called = 0

    async def fake_translate(bot_id, umo, system, user):
        seen_contexts.append(user)
        return json.dumps({"prompt": "hot cocoa at snowy station", "ref_ids": [], "aspect_ratio": "4:3"})

    async def rejecting_review(bot_id, umo, images, system, user):
        nonlocal review_called
        review_called += 1
        return {"pass": False, "reason": "杯口反光略怪", "fix_hint": "柔化高光", "description": "雪夜热可可"}

    pipeline = PhotoPipeline(
        db=db,
        configs=cfg,
        refs=refs,
        client=client,
        prompt_text=fake_translate,
        review_vision=rejecting_review,
        audit=audit,
        story_store=story_store,
        now_fn=lambda: now,
    )
    svc = ImageService(
        db=db, configs=cfg, refs=refs, temp=temp, client=client, pipeline=pipeline, audit=audit
    )

    # 1. 默认跳过视觉审核，切换提供商 p2，不带故事上下文，带模拟聊天
    res1 = await svc.test_photo(
        bot_id="bot1",
        scene="一杯热可可",
        provider_id="p2",
        include_story=False,
        recent_chat="小明：外面下雪了吗",
        run_review=False,
    )
    assert res1["ok"] is True
    assert res1["provider_id"] == "p2"
    assert res1["review"] is None
    assert review_called == 0
    assert "北方海港小镇" not in seen_contexts[0]
    assert "外面下雪了吗" in seen_contexts[0]
    assert [r["role"] for r in res1["refs"]] == ["style"]

    # 2. 勾选附带试跑视觉审核：即使审核驳回也不阻断原图、不触发重试
    res2 = await svc.test_photo(
        bot_id="bot1",
        scene="一杯热可可",
        include_story=True,
        run_review=True,
    )
    assert res2["ok"] is True
    assert res2["data"] == PNG_2
    assert res2["review"]["status"] == "rejected"
    assert res2["review"]["reason"] == "杯口反光略怪"
    assert review_called == 1
    assert len(client.calls) == 2  # 没有因审核驳回而重试
    assert "北方海港小镇" in seen_contexts[1]

    # 3. 不消耗每日配额，也不写入 photo_temp 或 image_refs 审核区
    used, _ = await svc.daily_usage("bot1")
    assert used == 0
    assert await db.scalar("SELECT COUNT(*) FROM image_temp", default=0) == 0
    assert (await refs.pending_summary("bot1"))["total"] == 0
    logs = await db.fetchall("SELECT kind, status FROM image_logs WHERE bot_id='bot1'")
    assert [row["kind"] for row in logs] == ["webui_test", "webui_test"]


def test_prompts_enforce_style_ref_and_drop_realism_bias():
    from astrbot_plugin_shadow_memory.imaging import prompts as img_prompts

    assert "真实手机" not in img_prompts.TRANSLATE_SYSTEM
    assert "普通前置自拍规范" in img_prompts.TRANSLATE_SYSTEM
    assert "不要**描写她手里举着手机对着自己" in img_prompts.TRANSLATE_SYSTEM
    assert "轮廓边缘与笔触类型" in img_prompts.TRANSLATE_SYSTEM
    assert "随季换装" in img_prompts.TRANSLATE_SYSTEM
    assert "画风规则" in img_prompts.AUTO_REF_SYSTEM
    assert "二次元" in img_prompts.REVIEW_SYSTEM

    user_with_vision = img_prompts.build_translate_user(
        scene_request="喝咖啡自拍",
        context_text="",
        catalog=[],
        max_subjects=4,
        has_style_refs=True,
        style_notes=["无黑描边柔和厚涂、灰调雾感"],
        attached_refs=["第 1 张：画风参考图「雾感厚涂」"],
    )
    assert "本次附带给你查看的参考图" in user_with_vision
    assert "无黑描边柔和厚涂" in user_with_vision
    assert "袖章" in user_with_vision
    assert "用户补充的画风要点" in user_with_vision

    user_text_only = img_prompts.build_translate_user(
        scene_request="喝咖啡",
        context_text="",
        catalog=[],
        max_subjects=4,
        has_style_refs=True,
    )
    assert "画风参考图（纯文本模式）" in user_text_only
    assert "不要在 prompt 中自行猜测" in user_text_only

    auto_with_style = img_prompts.build_auto_ref_user(
        category_label="人物",
        name="林雾",
        appearance="黑长直",
        has_style_refs=True,
        attached_refs=["第 1 张：画风参考图「赛璐璐」"],
    )
    assert "本次附带的画风参考图" in auto_with_style


async def test_multimodal_translate_modes_fallback_and_auto_ref(db, workdir, make_configs):
    audit = Audit(logging.getLogger("test"))
    base_cfg_data = {
        "image": {
            "enabled": True,
            "provider_id": "p1",
            "providers": [_provider_cfg()],
            "review_enabled": False,
            "translate_ref_mode": "style_and_self",
        }
    }
    cfg = make_configs(base_cfg_data)
    refs = RefStore(db, cfg, workdir)
    await refs.add_active("bot1", category="style", name="二次元画风", data=PNG_1, description="清透赛璐璐")
    self_ref = await refs.add_active("bot1", category="self", name="本人常服", data=PNG_2, description="银白长发")
    place_ref = await refs.add_active("bot1", category="place", name="老院子", data=PNG_1, description="石板小院")
    temp = PhotoTempStore(db, cfg, workdir)
    client = FakeClient()

    vision_calls: list[dict[str, Any]] = []
    text_calls: list[dict[str, Any]] = []

    async def ok_vision(bot_id, umo, system, user, images):
        vision_calls.append({"system": system, "user": user, "count": len(images)})
        return (
            json.dumps(
                {
                    "prompt": "2D anime cel-shaded illustration, silver-haired girl smiling in courtyard",
                    "ref_ids": [self_ref["id"], place_ref["id"]],
                    "aspect_ratio": "3:4",
                }
            ),
            True,
            "",
        )

    async def text_handler(bot_id, umo, system, user):
        text_calls.append({"system": system, "user": user})
        if "参考图筛选器" in system:
            return json.dumps({"ref_ids": [place_ref["id"]]})
        return json.dumps({"prompt": "courtyard scene", "ref_ids": [self_ref["id"]]})

    # 1. style_and_self（默认）：单次带图调用传入 style + self 共 2 张
    pipeline = PhotoPipeline(
        db=db,
        configs=cfg,
        refs=refs,
        client=client,
        prompt_text=text_handler,
        prompt_vision=ok_vision,
        review_vision=lambda *a: _coro(None),
        audit=audit,
    )
    svc = ImageService(
        db=db, configs=cfg, refs=refs, temp=temp, client=client, pipeline=pipeline, audit=audit
    )
    res = await svc.test_photo(bot_id="bot1", scene="院子里自拍")
    assert res["ok"] is True
    assert res["translate_meta"] == {
        "mode": "style_and_self",
        "used_vision": True,
        "ref_count": 2,
        "fallback_reason": "",
    }
    assert vision_calls[-1]["count"] == 2
    assert "画风参考图「二次元画风」" in vision_calls[-1]["user"]
    assert "角色本人参考图「本人常服」" in vision_calls[-1]["user"]

    # 2. style 模式：仅传画风图 1 张
    base_cfg_data["image"]["translate_ref_mode"] = "style"
    res_style = await svc.test_photo(bot_id="bot1", scene="院子里自拍")
    assert res_style["translate_meta"]["mode"] == "style"
    assert res_style["translate_meta"]["ref_count"] == 1
    assert vision_calls[-1]["count"] == 1

    # 3. selected 两阶段模式：先纯文本选 place_ref，再带图传 style + place_ref 共 2 张
    base_cfg_data["image"]["translate_ref_mode"] = "selected"
    text_calls.clear()
    res_sel = await svc.test_photo(bot_id="bot1", scene="拍一下空院子")
    assert len(text_calls) == 1 and "参考图筛选器" in text_calls[0]["system"]
    assert res_sel["translate_meta"]["used_vision"] is True
    assert res_sel["translate_meta"]["ref_count"] == 2
    assert "场景参考图「老院子」" in vision_calls[-1]["user"]

    # 4. auto_ref 也带画风参考图转译
    auto_out = await pipeline.run_auto_ref(
        bot_id="bot1",
        provider=_provider_cfg(),
        category="item",
        name="旧怀表",
        appearance="黄铜雕花怀表",
    )
    assert auto_out.prompt.startswith("2D anime")
    assert "本次附带的画风参考图" in vision_calls[-1]["user"]

    # 5. off 模式：不调视觉，纯文本附带用户画风说明
    base_cfg_data["image"]["translate_ref_mode"] = "off"
    before_v = len(vision_calls)
    text_calls.clear()
    res_off = await svc.test_photo(bot_id="bot1", scene="拍院子")
    assert len(vision_calls) == before_v
    assert res_off["translate_meta"] == {
        "mode": "off",
        "used_vision": False,
        "ref_count": 0,
        "fallback_reason": "",
    }
    assert "清透赛璐璐" in text_calls[-1]["user"]

    # 6. 纯文本模型原地降级：prompt_vision 返回 (None, False, reason) -> 以纯文本提示词重调 prompt_text
    base_cfg_data["image"]["translate_ref_mode"] = "style_and_self"

    async def unsupported_vision(bot_id, umo, system, user, images):
        return None, False, "模型 deepseek 不支持视觉，已降级纯文本"

    pipeline._prompt_vision = unsupported_vision
    text_calls.clear()
    res_fb = await svc.test_photo(bot_id="bot1", scene="拍院子")
    assert res_fb["ok"] is True
    assert res_fb["translate_meta"]["used_vision"] is False
    assert "不支持视觉" in res_fb["translate_meta"]["fallback_reason"]
    assert "本次附带给你查看的参考图" not in text_calls[-1]["user"]
    assert "清透赛璐璐" in text_calls[-1]["user"]


async def _coro(value):
    return value
