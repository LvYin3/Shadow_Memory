from __future__ import annotations

import pytest

from .helpers import build_stack, text_of_len


@pytest.fixture
def stack(db, configs):
    return build_stack(db, configs)


async def _add(stack, layer: str, text: str, sources=(1,)):
    return await stack.persona.apply_ops(
        "bot1",
        [{"op": "add", "layer": layer, "text": text, "source_event_ids": list(sources)}],
    )


async def test_render_is_ordered_core_edge_tension_without_labels(stack):
    await _add(stack, "tension", text_of_len(60, "悬着的事"))
    await _add(stack, "edge", text_of_len(60, "具体场合"))
    await _add(stack, "core", text_of_len(60, "骨子里的"))
    rendered = await stack.persona.render("bot1")
    lines = rendered.split("\n")
    assert len(lines) == 3
    assert lines[0].startswith("骨子里的")
    assert lines[1].startswith("具体场合")
    assert lines[2].startswith("悬着的事")
    assert "核心倾向" not in rendered and "【" not in rendered


async def test_add_respects_layer_quota(stack):
    applied = 0
    for index in range(7):
        result = await _add(stack, "core", text_of_len(60, f"第{index}条"))
        applied += result.applied
    counts = await stack.persona.counts("bot1")
    assert applied == 6
    assert counts["core"] == 6


async def test_entry_length_is_validated(stack):
    too_short = await _add(stack, "core", "太短了")
    assert too_short.applied == 0
    assert "长度" in too_short.rejected[0]


async def test_sources_are_required(stack):
    result = await stack.persona.apply_ops(
        "bot1", [{"op": "add", "layer": "core", "text": text_of_len(60), "source_event_ids": []}]
    )
    assert result.applied == 0
    assert "来源" in result.rejected[0]


async def test_delete_limit_and_rewrite_and_move(stack):
    await _add(stack, "core", text_of_len(60, "第一条"))
    await _add(stack, "core", text_of_len(60, "第二条"))
    first_id = (await stack.persona.entries("bot1"))[0].id

    moved = await stack.persona.apply_ops(
        "bot1", [{"op": "move", "id": first_id, "to_layer": "tension"}], now=1
    )
    assert moved.applied == 1
    entry = {item.id: item for item in await stack.persona.entries("bot1")}[first_id]
    assert entry.layer == "tension"

    rewritten = await stack.persona.apply_ops(
        "bot1",
        [
            {
                "op": "rewrite",
                "id": first_id,
                "text": text_of_len(60, "改过的"),
                "source_event_ids": [2],
            }
        ],
    )
    assert rewritten.applied == 1
    assert {item.id: item for item in await stack.persona.entries("bot1")}[first_id].text.startswith(
        "改过的"
    )

    second_id = (await stack.persona.entries("bot1"))[-1].id
    deleted = await stack.persona.apply_ops(
        "bot1",
        [
            {"op": "delete", "id": first_id},
            {"op": "delete", "id": second_id},
        ],
    )
    assert deleted.applied == 1
    assert any("删除" in reason for reason in deleted.rejected)


async def test_batch_over_hard_limit_is_rejected_whole(stack, make_configs):
    cfg = make_configs(
        {
            "persona": {
                "hard_chars": 200,
                "quota_core": 10,
                "entry_min_chars": 10,
                "entry_max_chars": 100,
            }
        }
    )
    local = build_stack(stack.db, cfg)
    ops = [
        {"op": "add", "layer": "core", "text": text_of_len(90, f"第{i}条"), "source_event_ids": [1]}
        for i in range(3)
    ]
    result = await local.persona.apply_ops("bot1", ops)
    assert result.applied == 0
    assert any("硬线" in reason for reason in result.rejected)
    assert await local.persona.entries("bot1") == []


async def test_snapshot_and_restore(stack):
    await _add(stack, "core", text_of_len(60, "原来的"))
    version = await stack.persona.snapshot("bot1", "test")
    await _add(stack, "edge", text_of_len(60, "后来的"))
    assert len(await stack.persona.entries("bot1")) == 2
    ok = await stack.persona.restore("bot1", version)
    assert ok is True
    entries = await stack.persona.entries("bot1")
    assert len(entries) == 1
    assert entries[0].text.startswith("原来的")


async def test_rollback_missing_version_returns_false(stack):
    assert await stack.persona.restore("bot1", 999) is False


async def test_prune_versions_keeps_recent(stack, make_configs):
    cfg = make_configs({"persona": {"snapshot_keep": 3}})
    local = build_stack(stack.db, cfg)
    for index in range(6):
        await local.persona.snapshot("bot1", f"v{index}")
    rows = await local.db.fetchall("SELECT id FROM persona_versions WHERE bot_id='bot1'")
    # 最近 3 个 + 每月首版（同月只多留最早的那次）
    assert 3 <= len(rows) <= 4


async def test_add_tension_respects_quota(stack):
    for index in range(3):
        assert await stack.persona.add_tension("bot1", text_of_len(60, f"悬{index}"), [1]) is not None
    assert await stack.persona.add_tension("bot1", text_of_len(60, "第四条"), [1]) is None
    counts = await stack.persona.counts("bot1")
    assert counts["tension"] == 3


async def test_char_count_matches_render(stack):
    await _add(stack, "core", text_of_len(60))
    assert await stack.persona.char_count("bot1") == len(await stack.persona.render("bot1"))
