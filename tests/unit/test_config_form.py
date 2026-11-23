"""配置页：分组、掩码、夹紧、密钥和按机器人覆盖。"""

from __future__ import annotations

import copy

import pytest

from astrbot_plugin_shadow_memory.webui.config_form import (
    FormError,
    apply,
    describe,
    load_schema,
    present,
    write_into,
)


def _fields(schema):
    found = {}
    for group in describe(schema):
        if group["kind"] != "sections":
            continue
        for section in group["sections"]:
            for block in section["blocks"]:
                for field in block["common"] + block["advanced"]:
                    found[field["path"]] = field
    return found


def test_groups_follow_the_six_blocks_and_the_level_rule():
    schema = load_schema()
    groups = {group["id"]: group for group in describe(schema)}
    assert list(groups)[:6] == ["model", "memory", "interaction", "story", "logging", "bots"]
    assert "other" not in groups
    fields = _fields(schema)
    assert fields["decision.provider_id"]["level"] == "common"
    assert fields["decision.provider_id"]["widget"] == "text"
    assert fields["decision.timeout_ms"]["level"] == "advanced"
    assert fields["text.temperature"]["level"] == "common"
    assert fields["text.max_tokens"]["level"] == "advanced"
    assert fields["text.thinking_by_task"]["widget"] == "thinking"
    assert fields["text.provider_id"]["widget"] == "provider"
    assert fields["dynamics.s_decay"]["level"] == "advanced"
    assert fields["recall.theta_pre"]["level"] == "common"
    assert fields["recall.theta_aux"]["level"] == "advanced"
    assert fields["interaction.media.dir"]["level"] == "advanced"
    assert fields["interaction.qzone.ugc_right"]["widget"] == "ugc"
    assert fields["interaction.qzone.action"]["level"] == "advanced"
    assert fields["story.quiet_hours"]["level"] == "common"
    assert fields["story.bucket_capacity"]["level"] == "advanced"
    assert fields["logging.retention_days"]["level"] == "common"
    assert fields["context.recent_messages"]["note"] == "按机器人的覆盖在设置页。"
    assert fields["interaction.gate_question"]["note"] == "按机器人的覆盖在设置页。"
    assert fields["interaction.tts_provider_ids"]["note"] == "按机器人的覆盖在设置页。"
    dynamics = next(
        section
        for section in groups["memory"]["sections"]
        if section["key"] == "dynamics"
    )
    assert all(not block["common"] for block in dynamics["blocks"])
    assert [field["key"] for field in groups["bots"]["fields"]][:3] == [
        "persona_mode",
        "base_card",
        "gate_enabled",
    ]


def test_unknown_top_level_key_lands_in_other():
    schema = load_schema()
    schema["future"] = {
        "type": "object",
        "description": "以后加的",
        "items": {"enabled": {"type": "bool", "description": "开关", "default": False}},
    }
    groups = {group["id"]: group for group in describe(schema)}
    assert groups["other"]["sections"][0]["key"] == "future"


def test_secrets_are_masked_and_blank_keeps_the_old_key():
    schema = load_schema()
    current = {"decision": {"api_key": "sk-live", "timeout_ms": 3000}}
    shown = present(schema, current)
    assert shown["secrets"]["decision.api_key"] == {"set": True}
    assert "sk-live" not in str(shown["fields"])
    updated = apply(schema, current, {"secrets": {"decision.api_key": {"value": "", "clear": False}}})
    assert updated["decision"]["api_key"] == "sk-live"
    replaced = apply(
        schema, current, {"secrets": {"decision.api_key": {"value": "sk-new", "clear": False}}}
    )
    assert replaced["decision"]["api_key"] == "sk-new"
    cleared = apply(
        schema, current, {"secrets": {"decision.api_key": {"value": "sk-new", "clear": True}}}
    )
    assert cleared["decision"]["api_key"] == ""


def test_slider_clamps_and_a_bad_number_rejects_the_whole_page():
    schema = load_schema()
    current = {"story": {"refill_per_day": 10, "unread_keep": 1}, "extra": {"keep": True}}
    updated = apply(schema, current, {"fields": {"story.refill_per_day": 999}})
    assert updated["story"]["refill_per_day"] == 60
    assert updated["extra"] == {"keep": True}
    assert current["story"]["refill_per_day"] == 10
    with pytest.raises(FormError, match="请填一个数字"):
        apply(schema, current, {"fields": {"story.unread_keep": "abc"}})
    assert current["story"]["unread_keep"] == 1


def test_override_blank_drops_the_key_and_keeps_unknown_ones():
    schema = load_schema()
    current = {
        "bot_overrides": {
            "bot1": {"persona_mode": "append", "custom_flag": "stay"},
            "bot2": {"gate_enabled": False},
        }
    }
    updated = apply(
        schema,
        current,
        {"bot_overrides": {"bot1": {"persona_mode": "", "base_card": "她"}}},
    )
    assert updated["bot_overrides"]["bot1"] == {"custom_flag": "stay", "base_card": "她"}
    assert updated["bot_overrides"]["bot2"] == {"gate_enabled": False}
    shown = present(schema, updated)
    assert "custom_flag" not in shown["bot_overrides"]["bot1"]
    assert shown["bot_overrides"]["bot1"]["base_card"] == "她"


def test_tts_rows_replace_and_thinking_keeps_unknown_tasks():
    schema = load_schema()
    current = {
        "interaction": {"tts_provider_ids": {"old": "tts-a", "keep": "tts-b"}},
        "text": {"thinking_by_task": {"fragment": "disabled", "custom": "enabled"}},
    }
    updated = apply(
        schema,
        current,
        {
            "tts_provider_ids": {"keep": "tts-b", "new": "tts-c", "": "nope"},
            "thinking_by_task": {"fragment": "auto"},
        },
    )
    assert updated["interaction"]["tts_provider_ids"] == {"keep": "tts-b", "new": "tts-c"}
    assert updated["text"]["thinking_by_task"]["fragment"] == "auto"
    assert updated["text"]["thinking_by_task"]["custom"] == "enabled"


def test_write_into_keeps_the_same_object():
    target = {"story": {"enabled": False}, "gone": 1}
    identity = target
    write_into(target, {"story": {"enabled": True}})
    assert target is identity
    assert target == {"story": {"enabled": True}}
    assert copy.deepcopy(target) == target
