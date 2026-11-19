from __future__ import annotations

import logging

import pytest

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.memory.ambient import AmbientService

from ..fakes import FakeJev

DAY = 86_400_000
NOW = 1_800_000_000_000


def _audit() -> Audit:
    logger = logging.getLogger("pm-test-ambient")
    logger.setLevel(logging.CRITICAL)
    return Audit(logger)


async def _event(
    db,
    *,
    event_id: int | None = None,
    fragment: str = "小明说搬家了",
    summary: str = "",
    salience: int = 3,
    ended_at: int = NOW,
    bot_id: str = "bot1",
) -> int:
    if event_id is None:
        return await db.execute(
            "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, fragment, summary, "
            "salience, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (bot_id, "umo1", "episodic", ended_at, ended_at, fragment, summary or None,
             salience, "closed", ended_at),
        )
    await db.execute(
        "INSERT INTO events(id, bot_id, umo, kind, started_at, ended_at, fragment, summary, "
        "salience, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, bot_id, "umo1", "episodic", ended_at, ended_at, fragment,
         summary or None, salience, "closed", ended_at),
    )
    return event_id


async def _turn(db, event_id: int, name: str, speaker_id: str = "u1", ts: int = NOW):
    await db.execute(
        "INSERT INTO event_turns(event_id, seq, role, speaker_id, speaker_name, text, ts) "
        "VALUES(?,?,?,?,?,?,?)",
        (event_id, 1, "user", speaker_id, name, "我搬家了", ts),
    )


@pytest.fixture
def ambient(db, configs):
    return AmbientService(db, configs, FakeJev(noul=0.8), _audit(), now_fn=lambda: NOW)


async def test_recent_uses_summary_fallback_and_budget(ambient, db):
    await _event(db, fragment="旧碎片", summary="", ended_at=NOW - DAY)
    await _event(db, fragment="新碎片", summary="小明说搬家了", ended_at=NOW)
    snap = await ambient.snapshot("bot1")
    assert snap.text.startswith("# 我这阵子的背景")
    assert "小明说搬家了" in snap.text
    assert "旧碎片" in snap.text
    assert len(snap.recent_event_ids) == 2
    assert len(snap.text) <= 400


async def test_important_is_excluded_from_recent_and_rendered(ambient, db):
    event_id = await _event(db, fragment="重要的事", summary="重要的事")
    await db.execute(
        "INSERT INTO ambient_memories(bot_id, kind, text, source_event_id, importance, "
        "active, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("bot1", "important", "重要的事", event_id, 0.9, 1, NOW, NOW),
    )
    snap = await ambient.snapshot("bot1")
    assert event_id not in snap.recent_event_ids
    assert event_id in snap.important_event_ids
    assert "重要的事" in snap.text


async def test_people_backfill_and_close_update(ambient, db):
    event_id = await _event(db, fragment="小明说搬家了")
    await _turn(db, event_id, "小明")
    snap = await ambient.snapshot("bot1")
    assert any(row["name"] == "小明" for row in snap.people)
    await ambient.on_event_closed(
        {
            "event_id": event_id,
            "bot_id": "bot1",
            "turns": [{"role": "user", "speaker_id": "u2", "speaker_name": "阿绿"}],
            "fragment": "阿绿说换工作了",
            "summary": "",
            "salience": 2,
            "now": NOW + 1,
        }
    )
    row = await db.fetchone(
        "SELECT name, last_topic FROM people WHERE bot_id='bot1' AND person_id='u2'"
    )
    assert row["name"] == "阿绿"
    assert len(row["last_topic"]) <= 20


async def test_promotion_requires_salience_and_threshold(db, configs):
    jev = FakeJev(noul=0.8)
    service = AmbientService(db, configs, jev, _audit(), now_fn=lambda: NOW)
    await service.on_event_closed(
        {
            "event_id": 1,
            "bot_id": "bot1",
            "turns": [],
            "fragment": "低 salience",
            "summary": "",
            "salience": 2,
            "now": NOW,
        }
    )
    assert await db.scalar("SELECT COUNT(*) FROM ambient_memories") == 0
    assert not jev.calls

    jev.noul_value = 0.5
    await service.on_event_closed(
        {
            "event_id": 2,
            "bot_id": "bot1",
            "turns": [],
            "fragment": "不够重要",
            "summary": "",
            "salience": 4,
            "now": NOW,
        }
    )
    assert await db.scalar("SELECT COUNT(*) FROM ambient_memories") == 0

    jev.noul_value = 0.8
    await service.on_event_closed(
        {
            "event_id": 3,
            "bot_id": "bot1",
            "turns": [],
            "fragment": "很重要的事",
            "summary": "",
            "salience": 4,
            "now": NOW,
        }
    )
    assert await db.scalar("SELECT COUNT(*) FROM ambient_memories") == 1


async def test_promotion_caps_active_important(db, configs):
    service = AmbientService(db, configs, FakeJev(noul=0.9), _audit(), now_fn=lambda: NOW)
    for index in range(7):
        await service.on_event_closed(
            {
                "event_id": index + 1,
                "bot_id": "bot1",
                "turns": [],
                "fragment": f"重要事情{index}",
                "summary": "",
                "salience": 4,
                "now": NOW + index,
            }
        )
    active = await db.scalar(
        "SELECT COUNT(*) FROM ambient_memories WHERE bot_id='bot1' AND active=1"
    )
    assert active == 5


async def test_disabled_snapshot_is_empty(db, make_configs):
    service = AmbientService(
        db,
        make_configs({"ambient": {"enabled": False}}),
        FakeJev(),
        _audit(),
        now_fn=lambda: NOW,
    )
    await _event(db, fragment="不该出现")
    snap = await service.snapshot("bot1")
    assert snap.text == ""
    assert snap.recent_event_ids == []
