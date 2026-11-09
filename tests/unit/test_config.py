from __future__ import annotations

from astrbot_plugin_shadow_memory.core.config import ConfigView, deep_merge


def test_defaults_are_complete():
    cfg = ConfigView({}, "bot1")
    assert cfg.str("decision.model") == ""
    assert cfg.str("decision.provider_id") == ""
    assert cfg.int("recall.max_candidates") == 12
    assert cfg.float("recall.theta_pre") == 0.4
    assert cfg.bool("interaction.gate_enabled") is True
    assert cfg.bool("interaction.environment.enabled") is True
    assert cfg.str("interaction.environment.prompt") == ""
    assert cfg.bool("interaction.say_example") is True
    assert cfg.bool("logging.debug") is False
    assert cfg.int("persona.quota_core") == 6


def test_global_override_wins_over_default():
    cfg = ConfigView({"recall": {"theta_pre": 0.55}}, "bot1")
    assert cfg.float("recall.theta_pre") == 0.55
    assert cfg.int("recall.max_candidates") == 12


def test_bot_override_maps_flat_keys():
    raw = {
        "bot_overrides": {
            "bot1": {
                "persona_mode": "plugin_only",
                "gate_threshold": 0.7,
                "theta_pre": 0.6,
                "qzone_daily_limit": 1,
                "tts_provider_id": "tts-abc",
                "environment_enabled": False,
                "environment_prompt": "- 我在测试。",
            }
        }
    }
    cfg = ConfigView(raw, "bot1")
    assert cfg.str("persona.mode") == "plugin_only"
    assert cfg.float("interaction.gate_threshold") == 0.7
    assert cfg.float("recall.theta_pre") == 0.6
    assert cfg.int("interaction.qzone.daily_limit") == 1
    assert cfg.section("interaction.tts_provider_ids")["bot1"] == "tts-abc"
    assert cfg.bool("interaction.environment.enabled") is False
    assert cfg.str("interaction.environment.prompt") == "- 我在测试。"


def test_bot_override_does_not_leak_to_other_bot():
    raw = {"bot_overrides": {"bot1": {"gate_threshold": 0.9}}}
    assert ConfigView(raw, "bot2").float("interaction.gate_threshold") == 0.5


def test_typed_accessors_clamp_and_fallback():
    cfg = ConfigView({"recall": {"theta_pre": 5.0}}, "bot")
    assert cfg.float("recall.theta_pre", lo=0.0, hi=1.0) == 1.0
    cfg2 = ConfigView({"recall": {"theta_pre": "nonsense"}}, "bot")
    assert cfg2.float("recall.theta_pre", default=0.4) == 0.4


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"b": 1}}
    override = {"a": {"c": 2}}
    merged = deep_merge(base, override)
    assert merged == {"a": {"b": 1, "c": 2}}
    assert base == {"a": {"b": 1}}
    assert override == {"a": {"c": 2}}


def test_unknown_bot_override_key_is_ignored():
    cfg = ConfigView({"bot_overrides": {"bot1": {"nope": 1}}}, "bot1")
    assert cfg.str("persona.mode") == "append"
