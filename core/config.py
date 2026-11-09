"""配置视图：默认值 → 全局配置 → 按机器人覆盖。"""

from __future__ import annotations

import copy
from typing import Any, Mapping

DEFAULT_VISION_PROMPT = (
    "用一句不超过 60 字的中文，客观描述这张图里有什么；不要猜测背景故事，不要加评论。"
)

DEFAULT_CONFIG: dict[str, Any] = {
    "decision": {
        "provider_id": "",
        "base_url": "",
        "api_key": "",
        "model": "",
        "protocol": "auto",
        "timeout_ms": 3000,
        "max_retries": 1,
    },
    "text": {
        "provider_id": "",
        "base_url": "",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.4,
        "max_tokens": 1200,
        "thinking": "auto",
        "reasoning_effort": "",
        "thinking_by_task": {
            "fragment": "disabled",
            "tension": "disabled",
            "facts": "disabled",
            "distill": "enabled",
            "consolidate": "enabled",
        },
    },
    "vision": {
        "enabled": True,
        "provider_id": "",
        "model": "deepseek-flash",
        "prompt": DEFAULT_VISION_PROMPT,
        "timeout_s": 20,
        "max_chars": 80,
        "max_tokens": 800,
        "disable_thinking": True,
    },
    "context": {
        "recent_messages": 20,
        "max_chars": 2000,
    },
    "memory": {
        "enabled": True,
        "idle_split_minutes": 30,
        "topic_end_threshold": 0.7,
        "big_event_chars": 20000,
        "big_event_turns": 80,
        "sweeper_seconds": 300,
        "turn_context_turns": 30,
    },
    "dynamics": {
        "s_decay": 0.999,
        "r_floor": 0.2,
        "recall_gain": 2.0,
        "gain_cap": 1.5,
        "archive_s": 1.0,
    },
    "edges": {"enabled": True, "max_degree": 20, "neighbors": 4},
    "recall": {
        "max_candidates": 12,
        "theta_pre": 0.40,
        "theta_aux": 0.35,
        "cooldown_turns": 10,
        "no_inject_last_turns": 3,
        "max_inject_chars": 40,
    },
    "consolidation": {
        "enabled": True,
        "cron": "30 3 * * *",
        "max_clusters": 5,
        "min_events": 3,
        "min_entities": 2,
        "window_days": 30,
        "lookback_days": 90,
        "max_events_per_cluster": 5,
    },
    "insights": {"enabled": True, "max_per_night": 2},
    "persona": {
        "mode": "append",
        "base_card": "",
        "max_chars": 1000,
        "warn_chars": 1300,
        "hard_chars": 1600,
        "quota_core": 6,
        "quota_edge": 7,
        "quota_tension": 3,
        "distill_days": 7,
        "distill_events": 20,
        "entry_min_chars": 50,
        "entry_max_chars": 70,
        "max_ops": 3,
        "max_deletes": 1,
        "snapshot_keep": 20,
    },
    "interaction": {
        "enabled": True,
        "gate_enabled": True,
        "gate_threshold": 0.50,
        "gate_history": 10,
        "max_segments": 3,
        "speak_max_chars": 60,
        "fallback_send_final_text": False,
        "environment": {
            "enabled": True,
            "prompt": "",
        },
        "say_example": True,
        "tts_provider_ids": {},
        "media": {
            "enabled": True,
            "dir": "qzone_media",
            "max_files": 100,
            "max_total_mb": 200,
            "max_image_mb": 5,
            "allow_event_images": True,
            "download_timeout_s": 10,
            "max_redirects": 3,
            "max_name_chars": 32,
            "max_description_chars": 200,
        },
        "send_image": {
            "enabled": True,
            "max_per_turn": 2,
            "max_caption_chars": 200,
        },
        "qzone": {
            "enabled": True,
            "action": "send_qzone_msg",
            "ugc_right": 1,
            "daily_limit": 3,
            "max_images": 9,
        },
    },
    "memo": {"enabled": True, "catalog_days": 7, "catalog_limit": 10},
    "logging": {"retention_days": 90, "debug": False},
    "bot_overrides": {},
}

# bot_overrides 里允许的扁平键 → 配置内路径
OVERRIDE_PATHS: dict[str, str] = {
    "persona_mode": "persona.mode",
    "base_card": "persona.base_card",
    "gate_enabled": "interaction.gate_enabled",
    "gate_threshold": "interaction.gate_threshold",
    "theta_pre": "recall.theta_pre",
    "theta_aux": "recall.theta_aux",
    "tts_provider_id": "interaction.tts_provider_ids",
    "environment_enabled": "interaction.environment.enabled",
    "environment_prompt": "interaction.environment.prompt",
    "qzone_daily_limit": "interaction.qzone.daily_limit",
    "qzone_ugc_right": "interaction.qzone.ugc_right",
}


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in (override or {}).items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _set_path(target: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def as_int(value: Any, default: int = 0, lo: int | None = None, hi: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if lo is not None:
        result = max(lo, result)
    if hi is not None:
        result = min(hi, result)
    return result


def as_float(
    value: Any, default: float = 0.0, lo: float | None = None, hi: float | None = None
) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if lo is not None:
        result = max(lo, result)
    if hi is not None:
        result = min(hi, result)
    return result


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


class ConfigView:
    """某个机器人视角下的只读配置。"""

    def __init__(self, raw: Mapping[str, Any] | None, bot_id: str = "") -> None:
        self.raw = dict(raw or {})
        self.bot_id = bot_id or ""
        merged = deep_merge(DEFAULT_CONFIG, self.raw)
        overrides = merged.get("bot_overrides")
        if self.bot_id and isinstance(overrides, Mapping):
            per_bot = overrides.get(self.bot_id)
            if isinstance(per_bot, Mapping):
                for key, value in per_bot.items():
                    path = OVERRIDE_PATHS.get(key)
                    if path is None:
                        continue
                    if path == "interaction.tts_provider_ids":
                        table = merged.setdefault("interaction", {}).setdefault(
                            "tts_provider_ids", {}
                        )
                        if isinstance(table, dict):
                            table[self.bot_id] = value
                        continue
                    _set_path(merged, path, value)
        self._merged = merged

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._merged
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    def section(self, dotted: str) -> dict[str, Any]:
        value = self.get(dotted, {})
        return dict(value) if isinstance(value, Mapping) else {}

    def bool(self, dotted: str, default: bool = False) -> bool:
        return as_bool(self.get(dotted, default), default)

    def int(self, dotted: str, default: int = 0, lo: int | None = None, hi: int | None = None) -> int:
        return as_int(self.get(dotted, default), default, lo, hi)

    def float(
        self, dotted: str, default: float = 0.0, lo: float | None = None, hi: float | None = None
    ) -> float:
        return as_float(self.get(dotted, default), default, lo, hi)

    def str(self, dotted: str, default: str = "") -> str:
        return as_str(self.get(dotted, default), default)

    def merged(self) -> dict[str, Any]:
        return copy.deepcopy(self._merged)


def pick_vision_provider_id(
    cfg: ConfigView, provider_settings: Mapping[str, Any] | None
) -> str:
    """视觉 provider 的三级优先级：插件配置 → AstrBot 打标 provider → 写作 provider。"""
    explicit = cfg.str("vision.provider_id").strip()
    if explicit:
        return explicit
    settings = provider_settings or {}
    fallback = str(settings.get("default_image_caption_provider_id") or "").strip()
    if fallback:
        return fallback
    return cfg.str("text.provider_id").strip()


THINKING_MODES = {"auto", "enabled", "disabled"}
REASONING_EFFORTS = {"low", "high", "max"}


def thinking_policy(cfg: ConfigView, task: str = "") -> tuple[str, str]:
    """返回 (思考模式, 强度)。

    任务级覆盖优先（空字符串表示跟随全局），非法值回退 auto / 空。
    """
    by_task = cfg.section("text.thinking_by_task")
    candidate = str(by_task.get(task) or "").strip().lower() if task else ""
    if candidate not in THINKING_MODES:
        candidate = cfg.str("text.thinking", "auto").strip().lower()
    mode = candidate if candidate in THINKING_MODES else "auto"
    effort = cfg.str("text.reasoning_effort", "").strip().lower()
    if effort not in REASONING_EFFORTS:
        effort = ""
    return mode, effort
