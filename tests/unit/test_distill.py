from __future__ import annotations

import pytest

from .helpers import build_stack, text_of_len

DAY = 86_400_000


@pytest.fixture
def stack(db, configs):
    return build_stack(db, configs)


async def _event(
    db,
    *,
    kind: str = "episodic",
    created: int = 0,
    bot: str = "bot1",
    fragment: str = "一段经历",
) -> int:
    return await db.execute(
        "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, fragment, entities, "
        "storage_strength, retrievability, last_recalled_at, salience, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (bot, "umo1", kind, created, created, fragment, "[]", 7.0, 1.0, created, 3, "closed", created),
    )


async def test_fresh_bot_is_not_due(stack):
    due, _ = await stack.distiller.due("bot1", 100 * DAY)
    assert due is False


async def test_due_after_seven_days(stack):
    await _event(stack.db, created=100 * DAY)
    due, reason = await stack.distiller.due("bot1", 107 * DAY)
    assert due is True
    assert "天" in reason


async def test_due_after_twenty_gists(stack):
    for index in range(20):
        await _event(stack.db, kind="gist", created=100 * DAY + index)
    due, reason = await stack.distiller.due("bot1", 100 * DAY + 2 * DAY)
    assert due is True
    assert "gist" in reason


async def test_no_sources_skips(stack):
    result = await stack.service.distill("bot1", "manual")
    assert result.skipped == "no_sources"


async def test_text_unavailable_skips(stack):
    await _event(stack.db, kind="gist", created=DAY)
    result = await stack.service.distill("bot1", "manual")
    assert result.skipped == "text_unavailable"


async def test_valid_ops_are_applied_with_snapshot(stack):
    gist = await _event(stack.db, kind="gist", created=DAY)
    stack.text.push(
        {
            "ops": [
                {
                    "op": "add",
                    "layer": "core",
                    "text": text_of_len(60, "我慢慢发现"),
                    "source_event_ids": [gist],
                }
            ]
        }
    )
    result = await stack.service.distill("bot1", "manual")
    assert result.applied == 1
    assert len(await stack.persona.entries("bot1")) == 1
    version = await stack.db.scalar("SELECT COUNT(*) FROM persona_versions")
    assert version == 1
    log = await stack.db.fetchone("SELECT * FROM distill_logs")
    assert log["applied"] == 1
    assert stack.text.tasks[-1] == "distill"


async def test_more_than_three_ops_are_rejected(stack):
    gist = await _event(stack.db, kind="gist", created=DAY)
    stack.text.push(
        {
            "ops": [
                {
                    "op": "add",
                    "layer": "edge",
                    "text": text_of_len(60, f"第{i}"),
                    "source_event_ids": [gist],
                }
                for i in range(5)
            ]
        }
    )
    result = await stack.service.distill("bot1", "manual")
    assert result.applied == 3
    assert len(result.rejected) == 2


async def test_two_deletes_are_rejected(stack):
    gist = await _event(stack.db, kind="gist", created=DAY)
    for index in range(2):
        await stack.persona.apply_ops(
            "bot1",
            [
                {
                    "op": "add",
                    "layer": "edge",
                    "text": text_of_len(60, f"第{index}"),
                    "source_event_ids": [gist],
                }
            ],
        )
    entries = await stack.persona.entries("bot1")
    stack.text.push(
        {
            "ops": [
                {"op": "delete", "id": entries[0].id},
                {"op": "delete", "id": entries[1].id},
            ]
        }
    )
    result = await stack.service.distill("bot1", "manual")
    assert result.applied == 1
    assert any("删除" in reason for reason in result.rejected)


async def test_missing_sources_are_rejected(stack):
    await _event(stack.db, kind="gist", created=DAY)
    stack.text.push(
        {"ops": [{"op": "add", "layer": "core", "text": text_of_len(60)}]}
    )
    result = await stack.service.distill("bot1", "manual")
    assert result.applied == 0
    assert any("来源" in reason for reason in result.rejected)


async def test_insight_is_marked_used(stack):
    await _event(stack.db, kind="gist", created=DAY)
    insight_id = await stack.insights.add("bot1", "我发现我很怕告别", [1], DAY)
    stack.text.push({"ops": []})
    await stack.service.distill("bot1", "manual")
    # 没有操作时不标记已用
    row = await stack.db.fetchone("SELECT used_in_version FROM insights WHERE id=?", (insight_id,))
    assert row["used_in_version"] is None

    stack.text.push(
        {
            "ops": [
                {
                    "op": "add",
                    "layer": "core",
                    "text": text_of_len(60, "我发现自己"),
                    "source_event_ids": [1],
                }
            ]
        }
    )
    await stack.service.distill("bot1", "manual")
    row = await stack.db.fetchone("SELECT used_in_version FROM insights WHERE id=?", (insight_id,))
    assert row["used_in_version"] is not None


async def test_tensions_are_sources(stack):
    await stack.persona.add_tension("bot1", "我还在想那天为什么先走。", [1])
    stack.text.push(
        {
            "ops": [
                {
                    "op": "rewrite",
                    "id": 1,
                    "text": text_of_len(60, "想明白了一点"),
                    "source_event_ids": [1],
                }
            ]
        }
    )
    result = await stack.service.distill("bot1", "manual")
    assert result.applied == 1
