"""角色视角世界常识：迁移默认、显式优先与自动分类、JEV 精筛与本地降级、防剧透与完整搬迁。"""

from __future__ import annotations

import json

import pytest

from astrbot_plugin_shadow_memory.core.db import Database
from astrbot_plugin_shadow_memory.story import carry
from astrbot_plugin_shadow_memory.story.knowledge import (
    format_world_turn_block,
    render_world_brief,
)

from .story_helpers import BOT, STORY_ON, build_story

FANTASY_SEED = {
    "version": "world_seed/v1",
    "title": "离光之外",
    "outline": (
        "这是一个日常水面下流淌着「时能」的和平世界。白映风在江岚大学读大一，"
        "平时驻守青溪县小院作为「繁星」巡查员，用自身时术「虚刃」切坯刻脉络。"
        "关于她血脉与旧日的秘密沉在平静日常之下，不会被轻易惊动。"
    ),
    "entries": [
        {
            "category": "其他",
            "name": "时能与宇宙流速",
            "body": "三维空间之外时间流动蕴含的能量称为「时能」，天体质量越大周围时能密度越低。",
        },
        {
            "category": "其他",
            "name": "流光异术与虚刃",
            "body": "时能生物获得的超自然能力称流光异术。白映风掌握「虚刃」，可进行高精度空间切割。",
        },
        {
            "category": "规则",
            "name": "保密与现世伪装",
            "body": "时管局、繁星、妖与时能均属绝密，对外维持走读大学生的普通口径。",
        },
        {
            "category": "其他",
            "name": "冥蛇族与能力封印",
            "body": "【世界底层秘密档案，白映风本人及外界均不知情】白映风实际上是冥蛇族最后的血脉，能力被封印。",
        },
        {
            "category": "规则",
            "name": "红线禁区与禁止揭秘",
            "body": "严格禁止在演出中揭晓白映风的冥蛇族血脉或唤醒被封印的真实能力。",
        },
    ],
    "hooks": ["西山竹林古道外围离光锁读数微小漂移"],
    "opening": {"where": "青溪县小院作坊小屋", "doing": "用虚刃修金属件"},
}


async def test_legacy_db_migration_adds_known_outline_and_defaults_canon_gm_only(workdir):
    db_path = workdir / "legacy_story.db"
    db = Database(db_path)
    await db.connect()
    try:
        # 模拟旧版无 known_outline / visibility 的表结构
        await db.execute("DROP TABLE story_canon")
        await db.execute("DROP TABLE story_state")
        await db.execute(
            "CREATE TABLE story_state ("
            "bot_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, "
            "paused INTEGER NOT NULL DEFAULT 0, pause_reason TEXT, "
            "tokens REAL NOT NULL DEFAULT 0, tokens_at INTEGER NOT NULL DEFAULT 0, "
            "card TEXT, next_wake_at INTEGER, last_scene_at INTEGER, "
            "failures INTEGER NOT NULL DEFAULT 0, world_title TEXT, outline TEXT, "
            "seed_meta TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        await db.execute(
            "CREATE TABLE story_canon ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, "
            "origin TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'other', "
            "name TEXT NOT NULL, body TEXT NOT NULL, scene_id INTEGER, "
            "created_at INTEGER NOT NULL)"
        )
        await db.execute(
            "INSERT INTO story_state(bot_id, enabled, world_title, outline, created_at, updated_at) "
            "VALUES(?, 1, '离光之外', '旧大纲', 1, 1)",
            (BOT,),
        )
        await db.execute(
            "INSERT INTO story_canon(bot_id, origin, category, name, body, created_at) "
            "VALUES(?, 'seed', 'other', '冥蛇族与能力封印', '主角不知情', 1)",
            (BOT,),
        )
    finally:
        await db.close()

    upgraded = Database(db_path)
    await upgraded.connect()
    try:
        state = await upgraded.fetchone("SELECT known_outline FROM story_state WHERE bot_id=?", (BOT,))
        assert state["known_outline"] is None
        row = await upgraded.fetchone("SELECT visibility FROM story_canon WHERE bot_id=?", (BOT,))
        assert row["visibility"] == "gm_only"
    finally:
        await upgraded.close()


async def test_auto_classify_on_legacy_seed_and_fail_closed(db, make_configs):
    env = build_story(db, make_configs(STORY_ON))
    # 1. textgen 返回合法分类结果：自动提炼底稿并准确隔离冥蛇族与红线禁区
    env.stack.text.push(
        {
            "known_outline": "我住在青溪县小院，在江岚大学走读，同时作为繁星巡查员用虚刃处理日常事务。",
            "entries": [
                {"name": "时能与宇宙流速", "visibility": "known"},
                {"name": "流光异术与虚刃", "visibility": "known"},
                {"name": "保密与现世伪装", "visibility": "known"},
                {"name": "冥蛇族与能力封印", "visibility": "gm_only"},
                {"name": "红线禁区与禁止揭秘", "visibility": "gm_only"},
            ],
        }
    )
    res = await env.service.import_seed(BOT, FANTASY_SEED)
    assert res["auto_classified"] is True
    assert res["known"] == 3 and res["gm_only"] == 2
    assert res["has_brief"] is True

    known_names = [row["name"] for row in await env.store.known_canon(BOT)]
    assert known_names == ["时能与宇宙流速", "流光异术与虚刃", "保密与现世伪装"]
    assert "冥蛇族与能力封印" not in known_names
    assert "红线禁区与禁止揭秘" not in known_names

    # 2. textgen 失败时保守降级：不泄露任何未审条目
    env.stack.text.queue.clear()
    res_fail = await env.service.import_seed(BOT, FANTASY_SEED)
    assert res_fail["auto_classified"] is False
    assert res_fail["known"] == 0 and res_fail["gm_only"] == 5
    assert await env.store.known_canon(BOT) == []


async def test_explicit_fields_take_precedence_without_calling_textgen(db, make_configs):
    env = build_story(db, make_configs(STORY_ON))
    explicit_seed = json.loads(json.dumps(FANTASY_SEED, ensure_ascii=False))
    explicit_seed["known_outline"] = "我自己写的角色已知底稿。"
    for item in explicit_seed["entries"]:
        item["visibility"] = "gm_only" if "冥蛇" in item["name"] or "红线" in item["name"] else "known"

    res = await env.service.import_seed(BOT, explicit_seed)
    assert res["auto_classified"] is False
    assert res["known"] == 3 and res["gm_only"] == 2
    assert env.stack.text.calls == []
    state = await env.store.state(BOT)
    assert state["known_outline"] == "我自己写的角色已知底稿。"


async def test_jev_rerank_and_local_fallback_never_leak_secrets(db, make_configs):
    env = build_story(db, make_configs(STORY_ON))
    explicit_seed = json.loads(json.dumps(FANTASY_SEED, ensure_ascii=False))
    explicit_seed["known_outline"] = "我在青溪县小院驻守，了解时能、虚刃与繁星。"
    for item in explicit_seed["entries"]:
        item["visibility"] = "gm_only" if "冥蛇" in item["name"] or "红线" in item["name"] else "known"
    await env.service.import_seed(BOT, explicit_seed)
    await env.service.enable(BOT)

    # 1. 常驻底稿与词条索引绝不含秘密条目
    sys_block = await env.service.world_system_block(BOT)
    assert "# 我知道的这个世界" in sys_block
    assert "时能与宇宙流速" in sys_block
    assert "流光异术与虚刃" in sys_block
    assert "冥蛇族" not in sys_block
    assert "红线禁区" not in sys_block

    # 2. JEV 精筛：即使隐晦提问未含原词，JEV 仍可命中已知条目，且秘密条目压根不进候选
    env.stack.jev.ask_value = {
        "w0": {"noul": 0.1},
        "w1": {"noul": 0.88},
        "w2": {"noul": 0.2},
    }
    block, recall = await env.service.world_turn_block(BOT, "你平时怎么切金属件？", [])
    assert recall.judged is True and recall.reason == "judged"
    assert len(recall.entries) == 1
    assert "流光异术与虚刃" in block
    assert "冥蛇族" not in block
    _, last_questions = env.stack.jev.calls[-1]
    assert all("冥蛇" not in q["instructions"] for q in last_questions.values())

    # 3. JEV 不可用时退回高精本地匹配：复合标题子词命中，日常闲聊不误触发
    env.stack.jev.available = False
    block_energy, recall_energy = await env.service.world_turn_block(BOT, "你知不知道时能是什么？", [])
    assert recall_energy.reason == "local_fallback"
    assert "时能与宇宙流速" in block_energy
    assert "第四维度" in block_energy or "时间流动" in block_energy

    block_casual, recall_casual = await env.service.world_turn_block(BOT, "今天中午吃什么呀", [])
    assert block_casual == "" and recall_casual.reason == "no_local_hit"

    # 4. 暂停时仍生效，关闭后停用
    await env.service.pause(BOT)
    assert await env.service.world_system_block(BOT) != ""
    assert await env.service.search_known_canon(BOT, "虚刃", 5)
    await env.service.disable(BOT)
    assert await env.service.world_system_block(BOT) == ""
    assert await env.service.search_known_canon(BOT, "虚刃", 5) == []


async def test_carry_preserves_known_outline_and_visibility(db, make_configs):
    env = build_story(db, make_configs(STORY_ON))
    explicit_seed = json.loads(json.dumps(FANTASY_SEED, ensure_ascii=False))
    explicit_seed["known_outline"] = "角色底稿内容"
    for item in explicit_seed["entries"]:
        item["visibility"] = "known" if item["name"] == "时能与宇宙流速" else "gm_only"
    await env.service.import_seed(BOT, explicit_seed)

    exported = await carry.export_world(db, BOT)
    await carry.replace_world(db, "bot2", exported, source=BOT, event_id_map={}, now=env.clock.now)

    state2 = await env.store.state("bot2")
    assert state2["known_outline"] == "角色底稿内容"
    canon2 = await env.store.canon("bot2")
    vis2 = {row["name"]: row["visibility"] for row in canon2}
    assert vis2["时能与宇宙流速"] == "known"
    assert vis2["冥蛇族与能力封印"] == "gm_only"
    assert render_world_brief("", []) == ""
    assert format_world_turn_block([]) == ""


def test_audit_accepts_kind_keyword_argument(caplog):
    import logging

    from astrbot_plugin_shadow_memory.core.audit import Audit

    logger = logging.getLogger("test-audit-kind")
    audit = Audit(logger, debug=True)
    with caplog.at_level(logging.DEBUG, logger="test-audit-kind"):
        audit.debug("recall_inject", bot_id=BOT, kind="fact", parts=1)
        audit.event("custom_event", bot_id=BOT, kind="world")
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "recall_inject" in messages and '"kind": "fact"' in messages
    assert "custom_event" in messages and '"kind": "world"' in messages
