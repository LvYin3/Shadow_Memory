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
    source: str = "chat",
) -> int:
    if event_id is None:
        return await db.execute(
            "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, fragment, summary, "
            "salience, status, source, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (bot_id, "umo1", "episodic", ended_at, ended_at, fragment, summary or None,
             salience, "closed", source, ended_at),
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


async def test_recent_skips_trivial_events(ambient, db):
    kept = await _event(db, fragment="小明说要搬家", salience=2, ended_at=NOW - DAY)
    trivial = await _event(db, fragment="打了个招呼", salience=1, ended_at=NOW)
    snap = await ambient.snapshot("bot1")
    assert snap.recent_event_ids == [kept]
    assert trivial not in snap.recent_event_ids


async def test_recent_keeps_room_for_chat_when_story_floods(ambient, db):
    """故事事件再多，“最近”里也要留两条聊天。"""
    chats = [
        await _event(db, fragment=f"和小明聊的第{index}件事", ended_at=NOW - 3 * DAY + index)
        for index in range(3)
    ]
    stories = [
        await _event(db, fragment=f"今天的第{index}段日常", ended_at=NOW - index, source="story")
        for index in range(6)
    ]
    snap = await ambient.snapshot("bot1")
    assert len(snap.recent_event_ids) == 5
    # 三条最新的故事 + 两条最新的聊天，按时间从新到旧
    assert snap.recent_event_ids == [stories[0], stories[1], stories[2], chats[2], chats[1]]


async def test_recent_chat_reservation_can_be_disabled(db, make_configs):
    service = AmbientService(
        db,
        make_configs({"ambient": {"recent_chat_min": 0}}),
        FakeJev(),
        _audit(),
        now_fn=lambda: NOW,
    )
    await _event(db, fragment="和小明聊天", ended_at=NOW - 3 * DAY)
    stories = [
        await _event(db, fragment=f"日常{index}", ended_at=NOW - index, source="story")
        for index in range(6)
    ]
    snap = await service.snapshot("bot1")
    assert snap.recent_event_ids == stories[:5]


async def test_recent_reservation_does_not_drop_story_when_there_is_room(ambient, db):
    chat = await _event(db, fragment="和小明聊天", ended_at=NOW - 2 * DAY)
    story = await _event(db, fragment="去了趟图书馆", ended_at=NOW, source="story")
    snap = await ambient.snapshot("bot1")
    assert snap.recent_event_ids == [story, chat]


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


async def test_backfill_takes_each_persons_latest_turn_only(ambient, db, monkeypatch):
    """回填在 SQL 里按人取最近一条，不把全部发言读进内存。"""
    old = await _event(db, fragment="小明说在找工作", ended_at=NOW - 5 * DAY)
    new = await _event(db, fragment="明明说找到工作了", ended_at=NOW - DAY)
    for index in range(120):
        await db.execute(
            "INSERT INTO event_turns(event_id, seq, role, speaker_id, speaker_name, text, ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (old, index + 1, "user", "u1", "小明", "……", NOW - 5 * DAY + index),
        )
    await _turn(db, new, "明明", speaker_id="u1", ts=NOW - DAY)  # 同一个人改了昵称
    await _turn(db, old, "阿绿", speaker_id="", ts=NOW - 5 * DAY)  # 只有名字、没有 id
    await db.execute(
        "INSERT INTO event_turns(event_id, seq, role, speaker_id, speaker_name, text, ts) "
        "VALUES(?,?,?,?,?,?,?)",
        (old, 999, "assistant", "", "", "我说的话不算人", NOW),
    )

    fetched: list[int] = []
    original = db.fetchall

    async def counting(sql, params=()):
        rows = await original(sql, params)
        if "event_turns" in sql:
            fetched.append(len(rows))
        return rows

    monkeypatch.setattr(db, "fetchall", counting)
    snap = await ambient.snapshot("bot1")
    assert fetched == [2]  # 122 条用户发言，只取回两个人各一行
    people = {row["person_id"]: row for row in await original("SELECT * FROM people")}
    assert set(people) == {"u1", "阿绿"}
    assert people["u1"]["name"] == "明明"
    assert people["u1"]["last_topic"] == "明明说找到工作了"
    assert people["u1"]["last_seen"] == NOW - DAY
    assert people["阿绿"]["last_topic"] == "小明说在找工作"
    assert {row["name"] for row in snap.people} == {"明明", "阿绿"}


async def test_backfill_leaves_known_people_alone(db, configs):
    """已经在 people 里的人由事件关闭时更新，重启后的回填不再覆盖他们。"""
    event_id = await _event(db, fragment="小明说搬家了")
    await _turn(db, event_id, "小明")
    await db.execute(
        "INSERT INTO people(bot_id, person_id, name, aliases, last_topic, last_seen, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        ("bot1", "u1", "小明", '["明明"]', "更新过的话题", NOW + 5, NOW, NOW),
    )
    service = AmbientService(db, configs, FakeJev(), _audit(), now_fn=lambda: NOW)
    await service.snapshot("bot1")
    row = await db.fetchone("SELECT * FROM people WHERE bot_id='bot1' AND person_id='u1'")
    assert (row["last_topic"], row["aliases"], row["last_seen"]) == (
        "更新过的话题",
        '["明明"]',
        NOW + 5,
    )
    # 另一个机器人的同名联系人不受影响，照常回填
    other = await _event(db, fragment="小明说要出差", bot_id="bot2")
    await _turn(db, other, "小明")
    await service.snapshot("bot2")
    assert await db.scalar("SELECT last_topic FROM people WHERE bot_id='bot2'") == "小明说要出差"


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
