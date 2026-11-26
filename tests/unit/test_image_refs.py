"""参考图库、审核区（单事物 3 / 总 15）与临时暂存区单测。"""

from __future__ import annotations

import pytest

from astrbot_plugin_shadow_memory.imaging.refs import RefError, RefStore
from astrbot_plugin_shadow_memory.imaging.temp import PhotoTempStore
from astrbot_plugin_shadow_memory.interaction.media_dir import MediaError

PNG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 32
PNG_B = b"\x89PNG\r\n\x1a\n" + b"B" * 32
PNG_C = b"\x89PNG\r\n\x1a\n" + b"C" * 32


async def test_active_ref_categories_and_caps(db, workdir, make_configs):
    cfg = make_configs({"image": {"max_style_refs": 2, "max_self_refs": 2}})
    store = RefStore(db, cfg, workdir)

    s1 = await store.add_active("bot1", category="style", name="胶片", data=PNG_A, description="暖色")
    s2 = await store.add_active("bot1", category="style", name="插画", data=PNG_B)
    assert len(await store.list_active("bot1", "style")) == 2
    with pytest.raises(RefError, match="画风参考图已达上限"):
        await store.add_active("bot1", category="style", name="超限", data=PNG_C)

    # self 允许多张直至上限
    await store.add_active("bot1", category="self", name="常服", data=PNG_A)
    await store.add_active("bot1", category="self", name="冬装", data=PNG_B)
    assert len(await store.list_active("bot1", "self")) == 2

    # person/place/item 同名上传替换旧图
    p1 = await store.add_active("bot1", category="person", name="林雾", data=PNG_A, description="短发")
    p2 = await store.add_active("bot1", category="person", name="林雾", data=PNG_B, description="长发")
    people = await store.list_active("bot1", "person")
    assert len(people) == 1
    assert people[0]["id"] == p2["id"]
    assert people[0]["description"] == "长发"
    assert not (store.active_dir("bot1").media_dir / p1["filename"]).exists()

    # style 不进入 active_catalog（由程序固定附带）
    catalog = await store.active_catalog("bot1")
    assert all(item["category"] != "style" for item in catalog)
    assert {item["id"] for item in catalog} >= {p2["id"]}
    assert s1["id"] not in {item["id"] for item in catalog} and s2["id"] not in {item["id"] for item in catalog}


async def test_pending_queue_caps_and_approve_cleans_siblings(db, workdir, make_configs):
    cfg = make_configs(
        {"image": {"ref": {"max_pending_per_subject": 3, "max_pending_total": 4}}}
    )
    store = RefStore(db, cfg, workdir)

    c1 = await store.add_pending("bot1", category="place", name="旧书店", data=PNG_A, prompt="p1")
    c2 = await store.add_pending("bot1", category="place", name="旧书店", data=PNG_B, prompt="p2")
    c3 = await store.add_pending("bot1", category="place", name="旧书店", data=PNG_C, prompt="p3")
    with pytest.raises(RefError, match="已达上限"):
        await store.add_pending("bot1", category="place", name="旧书店", data=PNG_A)

    # 再加 1 张不同事物，到达总上限 4
    await store.add_pending("bot1", category="item", name="怀表", data=PNG_A)
    ok, reason = await store.can_auto_generate("bot1", "item", "雨伞")
    assert not ok and "审核区已满" in reason
    with pytest.raises(RefError, match="审核区已满"):
        await store.add_pending("bot1", category="item", name="雨伞", data=PNG_A)

    # 批准 c2：移入正式目录，c1 和 c3 自动清理
    approved = await store.approve_pending("bot1", c2["id"], description="木架旧书店")
    assert approved["status"] == "active"
    assert approved["description"] == "木架旧书店"
    assert (store.active_dir("bot1").media_dir / approved["filename"]).exists()
    assert not (store.pending_dir("bot1").media_dir / c1["filename"]).exists()
    assert not (store.pending_dir("bot1").media_dir / c2["filename"]).exists()
    assert not (store.pending_dir("bot1").media_dir / c3["filename"]).exists()

    summary = await store.pending_summary("bot1")
    assert summary["total"] == 1
    assert summary["groups"][0]["name"] == "怀表"


async def test_photo_temp_store_save_read_and_expire(db, workdir, make_configs):
    clock = [1_000_000]
    cfg = make_configs({"image": {"temp_ttl_hours": 1}})
    temp = PhotoTempStore(db, cfg, workdir, now_fn=lambda: clock[0])

    saved = await temp.save("bot1", PNG_A, prompt="desk", description="书桌")
    assert saved["handle"].startswith("photo:")
    assert await temp.read("bot1", saved["id"]) == PNG_A

    # 跨 bot 隔离
    with pytest.raises(MediaError):
        await temp.read("bot2", saved["id"])

    # 过期后不可读且被 cleanup 清理
    clock[0] += 3_600_000 + 1
    with pytest.raises(MediaError, match="过期"):
        await temp.read("bot1", saved["id"])
    removed = await temp.cleanup()
    assert removed == 1
    assert not (temp.temp_dir("bot1").media_dir / saved["filename"]).exists()
