from __future__ import annotations

import json

import pytest

from astrbot_plugin_shadow_memory.memory.seed import SeedError, validate_seed

from .helpers import build_stack, text_of_len

DAY = 86_400_000


@pytest.fixture
def stack(db, configs):
    return build_stack(db, configs)


def seed_payload(**overrides):
    payload = {
        "version": "persona_seed/v1",
        "bot_hint": "bot1",
        "persona": {
            "core": [text_of_len(60, "我骨子里")],
            "edge": [text_of_len(60, "在人多的场合")],
            "tension": [],
        },
        "facts": [
            {
                "kind": "world",
                "subject_id": "u1",
                "text": "小明住在杭州",
                "key_fact_type": "none",
                "salience": 3,
            }
        ],
        "events": [
            {
                "fragment": "第一次见面是在雨天",
                "summary": "",
                "entities": ["小明", "雨"],
                "occurred_days_ago": 3,
                "salience": 3,
                "participants": [{"id": "u1", "name": "小明"}],
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_validate_accepts_good_seed(configs):
    normalized = validate_seed(seed_payload(), configs("bot1"))
    assert len(normalized["persona"]["core"]) == 1
    assert normalized["facts"][0]["kind"] == "world"


def test_validate_rejects_short_persona(configs):
    payload = seed_payload()
    payload["persona"]["core"] = ["太短"]
    with pytest.raises(SeedError):
        validate_seed(payload, configs("bot1"))


def test_validate_rejects_too_many_entries(configs):
    payload = seed_payload()
    payload["persona"]["core"] = [text_of_len(60, f"第{i}") for i in range(10)]
    with pytest.raises(SeedError):
        validate_seed(payload, configs("bot1"))


def test_validate_rejects_bad_event_fragment(configs):
    payload = seed_payload()
    payload["events"][0]["fragment"] = "太长" * 40
    with pytest.raises(SeedError):
        validate_seed(payload, configs("bot1"))


async def test_import_into_empty_memory(stack):
    counts = await stack.service.import_seed("bot1", seed_payload())
    assert counts == {"persona": 2, "facts": 1, "events": 1}
    entries = await stack.persona.entries("bot1")
    assert len(entries) == 2
    fact = await stack.db.fetchone("SELECT * FROM facts")
    assert fact["text"] == "小明住在杭州"
    event = await stack.db.fetchone("SELECT * FROM events")
    assert event["kind"] == "episodic"
    assert event["status"] == "closed"
    assert event["storage_strength"] == pytest.approx(8.75)


async def test_import_refuses_non_empty_without_force(stack):
    await stack.service.import_seed("bot1", seed_payload())
    with pytest.raises(SeedError):
        await stack.service.import_seed("bot1", seed_payload())


async def test_force_import_replaces_and_snapshots(stack):
    await stack.service.import_seed("bot1", seed_payload())
    payload = seed_payload()
    payload["persona"]["core"] = [text_of_len(60, "重来一次")]
    counts = await stack.service.import_seed("bot1", payload, force=True)
    assert counts["persona"] == 2
    entries = await stack.persona.entries("bot1")
    assert any(entry.text.startswith("重来一次") for entry in entries)
    versions = await stack.db.fetchall("SELECT reason FROM persona_versions WHERE bot_id='bot1'")
    assert any("seed" in row["reason"] for row in versions)
    assert await stack.db.scalar("SELECT COUNT(*) FROM events") == 1


async def test_export_roundtrip(stack):
    await stack.service.import_seed("bot1", seed_payload())
    exported = await stack.service.export_seed("bot1")
    assert exported["version"].startswith("persona_seed")
    assert exported["facts"][0]["text"] == "小明住在杭州"
    assert exported["events"][0]["fragment"] == "第一次见面是在雨天"


async def test_export_is_importable(stack):
    await stack.service.import_seed("bot1", seed_payload())
    exported = await stack.service.export_seed("bot1")
    counts = await stack.service.import_seed("bot2", exported)
    assert counts["events"] == 1
    assert counts["persona"] == 2


async def test_import_is_bot_scoped(stack):
    await stack.service.import_seed("bot1", seed_payload())
    assert await stack.persona.entries("bot2") == []
    assert await stack.db.scalar("SELECT COUNT(*) FROM events WHERE bot_id='bot2'") == 0


async def test_full_export_contains_events_and_persona(stack):
    await stack.service.import_seed("bot1", seed_payload())
    payload = await stack.service.export_full("bot1")
    assert payload["version"] == "shadow_memory/full/v2"
    assert payload["events"] and payload["persona"]
    assert "insights" in payload and "notes" in payload


def test_fact_key_type_maps_to_key_fact(configs):
    payload = seed_payload()
    payload["facts"][0]["key_fact_type"] = "time"
    normalized = validate_seed(payload, configs("bot1"))
    assert normalized["facts"][0]["key_fact"] is True


def test_json_roundtrip_of_export():
    payload = seed_payload()
    assert json.loads(json.dumps(payload, ensure_ascii=False))["version"] == "persona_seed/v1"
