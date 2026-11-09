from __future__ import annotations

import json

import pytest

from astrbot_plugin_shadow_memory.memory.textutil import trim_at_boundary

from ..fakes import FakeJev
from .helpers import build_stack

DAY = 86_400_000


async def _event(
    db,
    fragment: str,
    entities: list[str],
    *,
    strength: float = 7.0,
    last: int | None = None,
    ended: int = 0,
    salience: int = 3,
    bot: str = "bot1",
    status: str = "closed",
) -> int:
    return await db.execute(
        "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, fragment, entities, "
        "storage_strength, retrievability, last_recalled_at, salience, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            bot,
            "umo1",
            "episodic",
            ended,
            ended,
            fragment,
            json.dumps(entities, ensure_ascii=False),
            strength,
            1.0,
            last,
            salience,
            status,
            ended,
        ),
    )


@pytest.fixture
def stack(db, configs):
    return build_stack(db, configs)


async def test_candidates_require_r_above_floor(stack):
    now = 100 * DAY
    await _event(stack.db, "关于海边的记忆", ["海边"], strength=1.0, last=now - 20 * DAY, ended=now)
    candidates, below = await stack.recall.candidates("bot1", "海边", now)
    assert candidates == []
    assert below == 1


async def test_candidates_are_capped(stack):
    now = 100 * DAY
    for index in range(20):
        await _event(stack.db, f"海边的第{index}次", ["海边"], last=now - DAY, ended=now)
    candidates, _ = await stack.recall.candidates("bot1", "海边", now)
    assert 0 < len(candidates) <= 12


async def test_neighbor_is_pulled_in(stack):
    now = 100 * DAY
    hit = await _event(stack.db, "海边风很大", ["海边", "风"], last=now - DAY, ended=now)
    other = await _event(stack.db, "后来聊到别的事", ["风"], last=now - DAY, ended=now)
    await stack.edges.link("bot1", hit, ["海边", "风"], now)
    await stack.edges.link("bot1", other, ["风"], now)
    candidates, _ = await stack.recall.candidates("bot1", "海边", now)
    ids = {candidate.event_id for candidate in candidates}
    assert hit in ids
    assert other in ids


async def test_archived_and_merged_are_excluded(stack):
    now = 100 * DAY
    await _event(stack.db, "海边的旧事", ["海边"], last=now - DAY, ended=now, status="archived")
    merged = await _event(stack.db, "海边被并掉了", ["海边"], last=now - DAY, ended=now)
    await stack.db.execute("UPDATE events SET merged_into=999 WHERE id=?", (merged,))
    candidates, _ = await stack.recall.candidates("bot1", "海边", now)
    assert candidates == []


async def test_decide_none_wins(stack):
    now = 100 * DAY
    await _event(stack.db, "海边风很大", ["海边"], last=now - DAY, ended=now)
    stack.jev.choice_value = {"none": 0.8, "c0": 0.2}
    injection = await stack.recall.decide("bot1", "umo1", "海边", [], now)
    assert injection.reason == "none_wins"
    assert injection.main is None


async def test_decide_below_threshold(stack):
    now = 100 * DAY
    await _event(stack.db, "海边风很大", ["海边"], last=now - DAY, ended=now)
    stack.jev.choice_value = {"none": 0.6, "c0": 0.3}
    injection = await stack.recall.decide("bot1", "umo1", "海边", [], now)
    assert injection.reason == "none_wins"  # none 仍是最大值

    stack.jev.choice_value = {"none": 0.30, "c0": 0.39}
    injection = await stack.recall.decide("bot1", "umo1", "海边", [], now)
    assert injection.reason == "below_threshold"


async def test_decide_injects_top_candidate(stack):
    now = 100 * DAY
    event = await _event(stack.db, "海边风很大", ["海边"], last=now - DAY, ended=now)
    stack.jev.choice_value = {"none": 0.1, "c0": 0.9}
    injection = await stack.recall.decide("bot1", "umo1", "海边", [], now)
    assert injection.reason == "injected"
    assert injection.main is not None and injection.main.event_id == event


async def test_decide_without_provider(stack):
    now = 100 * DAY
    await _event(stack.db, "海边风很大", ["海边"], last=now - DAY, ended=now)
    stack.jev.available = False
    injection = await stack.recall.decide("bot1", "umo1", "海边", [], now)
    assert injection.reason == "no_provider"


async def test_decide_judge_failure(stack):
    now = 100 * DAY
    await _event(stack.db, "海边风很大", ["海边"], last=now - DAY, ended=now)
    stack.jev.choice_value = None
    stack.jev.ask = None
    stack.jev.choice = _failing_choice
    stack.jev.available = True
    injection = await stack.recall.decide("bot1", "umo1", "海边", [], now)
    assert injection.reason == "judge_failed"


async def _failing_choice(state, instructions, options):
    return None


async def test_veto_short_circuits(stack):
    injection = await stack.recall.decide("bot1", "umo1", "海边", [], 1, veto_reason="cooldown")
    assert injection.reason == "cooldown"


async def test_apply_recall_grows_strength_and_resets_r(stack):
    now = 100 * DAY
    event = await _event(stack.db, "海边风很大", ["海边"], strength=8.0, last=now - 7 * DAY, ended=now)
    candidates, _ = await stack.recall.candidates("bot1", "海边", now)
    assert candidates
    await stack.recall.apply_recall(candidates[0], now)
    row = await stack.db.fetchone("SELECT * FROM events WHERE id=?", (event,))
    assert row["storage_strength"] > 8.0
    assert row["retrievability"] == 1.0
    assert row["last_recalled_at"] == now


async def test_aux_returns_none_without_tension(stack):
    assert await stack.recall.aux("bot1", 1) is None


async def test_aux_injects_tension(stack):
    await stack.persona.add_tension("bot1", "我还是没想明白那天为什么要先走。", [1])
    stack.jev.choice_value = {"none": 0.05, "t0": 0.95}
    aux = await stack.recall.aux("bot1", 1)
    assert aux is not None
    assert len(aux) <= 40


def test_trim_at_boundary_prefers_sentence_end():
    body = "第一句话说完。第二句话还没说完"
    assert trim_at_boundary(body, 8) == "第一句话说完。"
    assert trim_at_boundary("短句", 10) == "短句"
