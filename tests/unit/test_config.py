from __future__ import annotations

from astrbot_plugin_shadow_memory.core.config import (
    DEFAULT_GATE_QUESTION,
    ConfigView,
    deep_merge,
    resolve_gate_question,
)
from astrbot_plugin_shadow_memory.memory import prompts


def test_defaults_are_complete():
    cfg = ConfigView({}, "bot1")
    assert cfg.str("decision.model") == ""
    assert cfg.str("decision.provider_id") == ""
    assert cfg.int("recall.max_candidates") == 12
    assert cfg.float("recall.theta_pre") == 0.4
    assert cfg.bool("interaction.gate_enabled") is True
    assert cfg.str("interaction.gate_question") == DEFAULT_GATE_QUESTION
    assert cfg.bool("interaction.environment.enabled") is True
    assert cfg.str("interaction.environment.prompt") == ""
    assert cfg.bool("interaction.say_example") is True
    assert cfg.bool("logging.debug") is False
    assert cfg.bool("ambient.enabled") is True
    assert cfg.int("ambient.recent_days") == 7
    assert cfg.int("ambient.recent_limit") == 5
    assert cfg.float("ambient.promote_threshold") == 0.75
    assert cfg.int("ambient.min_salience") == 3
    assert cfg.bool("recall.skip_smalltalk") is True
    assert cfg.int("recall.min_query_chars") == 2
    assert cfg.int("persona.distill_retry_minutes") == 360
    assert cfg.int("persona.distill_check_seconds") == 300
    assert cfg.bool("decision.full_context") is True
    assert cfg.int("decision.full_context_max_chars") == 16000
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
                "gate_question": "Should I reply?",
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
    assert cfg.str("interaction.gate_question") == "Should I reply?"
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


def test_gate_question_alias_keeps_builtin_text():
    assert prompts.REPLY_NOW_INSTRUCTION == DEFAULT_GATE_QUESTION
    assert DEFAULT_GATE_QUESTION.startswith("Given the recent group messages")


def test_gate_question_defaults_to_builtin():
    assert resolve_gate_question(ConfigView({}, "bot1")) == (
        DEFAULT_GATE_QUESTION,
        "builtin",
    )
    # 升级后内置原文会落盘到配置：与内置一致仍算 builtin
    saved = ConfigView({"interaction": {"gate_question": DEFAULT_GATE_QUESTION}}, "bot1")
    assert resolve_gate_question(saved) == (DEFAULT_GATE_QUESTION, "builtin")


def test_gate_question_priority_webui_bot_config():
    raw = {
        "interaction": {"gate_question": "global question"},
        "bot_overrides": {"bot1": {"gate_question": "bot question"}},
    }
    assert resolve_gate_question(ConfigView(raw, "bot1"), "webui question") == (
        "webui question",
        "webui",
    )
    assert resolve_gate_question(ConfigView(raw, "bot1")) == (
        "bot question",
        "bot_override",
    )
    assert resolve_gate_question(ConfigView(raw, "bot2")) == (
        "global question",
        "config",
    )
    assert resolve_gate_question(ConfigView(raw, "")) == ("global question", "config")


def test_gate_question_blank_layers_fall_through():
    raw = {
        "interaction": {"gate_question": "global question"},
        "bot_overrides": {"bot1": {"gate_question": "   "}},
    }
    # 空的机器人覆盖在合并结果里会盖掉全局值，解析时必须回落到全局
    assert ConfigView(raw, "bot1").str("interaction.gate_question") == "   "
    assert resolve_gate_question(ConfigView(raw, "bot1"), "  \n ") == (
        "global question",
        "config",
    )
    blank = {
        "interaction": {"gate_question": ""},
        "bot_overrides": {"bot1": {"gate_question": None}},
    }
    assert resolve_gate_question(ConfigView(blank, "bot1"), None) == (
        DEFAULT_GATE_QUESTION,
        "builtin",
    )


def test_gate_question_is_trimmed_and_newlines_normalized():
    raw = {"interaction": {"gate_question": "  line one\r\nline two \n"}}
    assert resolve_gate_question(ConfigView(raw, "bot1")) == (
        "line one\nline two",
        "config",
    )
