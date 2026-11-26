"""插件配置页：从 schema 分组，并按页上的提交写回同一份配置。

不读 AstrBot。密钥不回显；留空不改，只有「清除」才写成空。带 slider 的数字夹进范围内。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "_conf_schema.json"

SETTINGS_NOTE = "按机器人的覆盖在设置页。"

# 顶层键 → 页面上的块。没点名的顶层键进「其他」。
GROUP_DEFS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("model", "模型", ("decision", "text", "vision")),
    (
        "memory",
        "记忆",
        (
            "context",
            "memory",
            "dynamics",
            "edges",
            "recall",
            "ambient",
            "consolidation",
            "insights",
            "persona",
        ),
    ),
    ("interaction", "互动", ("interaction", "memo")),
    ("story", "故事", ("story",)),
    ("image", "生图", ("image",)),
    ("logging", "日志", ("logging",)),
)

# 数字里要放在常用的主旋钮。开关、下拉、密钥、多行和普通字符串默认就是常用。
KNOBS = frozenset(
    {
        "text.temperature",
        "context.recent_messages",
        "memory.idle_split_minutes",
        "recall.theta_pre",
        "recall.candidate_pool",
        "ambient.recent_days",
        "ambient.recent_chat_min",
        "insights.max_per_night",
        "interaction.gate_threshold",
        "interaction.qzone.daily_limit",
        "memo.catalog_days",
        "story.refill_per_day",
        "story.share_per_day",
        "story.unread_keep",
        "story.npc_active_max",
        "story.npc_idle_days",
        "image.daily_limit",
        "image.max_retries",
        "image.ref.max_pending_per_subject",
        "image.ref.max_pending_total",
        "logging.retention_days",
    }
)

# 字符串里仍收进进阶的两项。
ADVANCED_PATHS = frozenset(
    {
        "interaction.media.dir",
        "interaction.qzone.action",
    }
)

NOTE_PATHS = frozenset(
    {
        "context.recent_messages",
        "interaction.gate_question",
        "interaction.tts_provider_ids",
    }
)

THINKING_TASKS = ("fragment", "tension", "facts", "distill", "consolidate")
THINKING_MODES = ("", "auto", "enabled", "disabled")
UGC_OPTIONS = (
    (1, "1 所有人"),
    (4, "4 好友"),
    (64, "64 仅自己"),
)

# 按机器人覆盖的键，顺序与 core.config.OVERRIDE_PATHS 一致。
OVERRIDE_PATHS = {
    "persona_mode": "persona.mode",
    "base_card": "persona.base_card",
    "gate_enabled": "interaction.gate_enabled",
    "gate_threshold": "interaction.gate_threshold",
    "gate_question": "interaction.gate_question",
    "theta_pre": "recall.theta_pre",
    "theta_aux": "recall.theta_aux",
    "tts_provider_id": "interaction.tts_provider_ids",
    "environment_enabled": "interaction.environment.enabled",
    "environment_prompt": "interaction.environment.prompt",
    "qzone_daily_limit": "interaction.qzone.daily_limit",
    "qzone_ugc_right": "interaction.qzone.ugc_right",
    "story_base_card": "story.base_card",
    "image_enabled": "image.enabled",
    "image_provider_id": "image.provider_id",
}


class FormError(ValueError):
    """整页不写。message 里写明是哪一项。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("；".join(self.errors) or "配置没有通过校验")


def load_schema(path: Path | None = None) -> dict[str, Any]:
    raw = json.loads((path or SCHEMA_PATH).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise FormError(["配置 schema 不是对象"])
    return raw


def describe(schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    """六块（以及必要时的「其他」）和按机器人覆盖的字段。"""
    sections = {
        key: _section(key, spec)
        for key, spec in schema.items()
        if key != "bot_overrides" and isinstance(spec, dict)
    }
    groups: list[dict[str, Any]] = []
    used: set[str] = set()
    for group_id, title, keys in GROUP_DEFS:
        picked = []
        for key in keys:
            section = sections.get(key)
            if section is not None:
                picked.append(section)
                used.add(key)
        if picked:
            groups.append({"id": group_id, "title": title, "kind": "sections", "sections": picked})
    groups.append(
        {
            "id": "bots",
            "title": "按机器人覆盖",
            "kind": "overrides",
            "fields": _override_fields(schema),
        }
    )
    others = [sections[key] for key in sections if key not in used]
    if others:
        groups.append({"id": "other", "title": "其他", "kind": "sections", "sections": others})
    return groups


def present(schema: Mapping[str, Any], current: Mapping[str, Any] | None) -> dict[str, Any]:
    """给页面的值。密钥只报有没有填过。"""
    raw = dict(current or {})
    fields: dict[str, Any] = {}
    secrets: dict[str, dict[str, bool]] = {}
    thinking: dict[str, str] | None = None
    tts: dict[str, str] | None = None
    for field in _iter_fields(schema):
        path = field["path"]
        value = _get(raw, path, field.get("default"))
        widget = field["widget"]
        if widget == "secret":
            secrets[path] = {"set": bool(str(value or "").strip())}
        elif widget == "thinking":
            thinking = _present_thinking(value, field.get("default"))
        elif widget == "tts":
            tts = _present_tts(value)
        elif widget == "dict_rows":
            fields[path] = _present_dict(value)
        elif widget == "bool":
            fields[path] = _as_bool(value, bool(field.get("default")))
        elif widget == "ugc":
            fields[path] = _as_ugc(value, field.get("default"))
        elif widget == "number":
            fields[path] = value if value is not None else field.get("default")
        else:
            fields[path] = "" if value is None else value
    overrides: dict[str, dict[str, Any]] = {}
    table = raw.get("bot_overrides")
    override_fields = {field["key"]: field for field in _override_fields(schema)}
    if isinstance(table, Mapping):
        for bot, data in table.items():
            if not isinstance(data, Mapping):
                continue
            shown = {}
            for key in OVERRIDE_PATHS:
                if key not in data:
                    continue
                field = override_fields.get(key)
                value = data[key]
                if field and field["widget"] == "tri":
                    value = _as_bool(value, False)
                shown[key] = value
            overrides[str(bot)] = shown
    return {
        "fields": fields,
        "secrets": secrets,
        "thinking_by_task": thinking if thinking is not None else {},
        "tts_provider_ids": tts if tts is not None else {},
        "bot_overrides": overrides,
    }


def apply(
    schema: Mapping[str, Any],
    current: Mapping[str, Any] | None,
    submitted: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """返回写回用的新配置。current 不动。有一项不合法就整页拒绝。"""
    result = copy.deepcopy(dict(current or {}))
    payload = dict(submitted or {})
    incoming = payload.get("fields") if isinstance(payload.get("fields"), Mapping) else {}
    secrets = payload.get("secrets") if isinstance(payload.get("secrets"), Mapping) else {}
    errors: list[str] = []
    for field in _iter_fields(schema):
        path = field["path"]
        label = field["label"]
        widget = field["widget"]
        try:
            if widget == "secret":
                _apply_secret(result, path, secrets.get(path))
            elif widget == "thinking":
                if "thinking_by_task" in payload:
                    _apply_thinking(result, path, payload.get("thinking_by_task"), label, errors)
            elif widget == "tts":
                if "tts_provider_ids" in payload:
                    _apply_tts(result, path, payload.get("tts_provider_ids"), label, errors)
            elif widget == "dict_rows":
                if path in incoming:
                    _apply_dict(result, path, incoming.get(path), label, errors)
            elif path in incoming:
                _apply_value(result, field, incoming.get(path), errors)
        except FormError:
            raise
        except (TypeError, ValueError):
            errors.append(f"{label}：没有通过校验")
    _apply_overrides(schema, result, payload.get("bot_overrides"), errors)
    if errors:
        raise FormError(errors)
    return result


def write_into(target: dict[str, Any], updated: Mapping[str, Any]) -> None:
    """就地换成新内容，保留原来的配置对象（AstrBotConfig.save_config 靠它）。"""
    for key in list(target.keys()):
        if key not in updated:
            del target[key]
    for key, value in updated.items():
        target[key] = value


def _section(key: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    if spec.get("type") == "object" or isinstance(spec.get("items"), Mapping):
        blocks.extend(_blocks(key, spec))
    else:
        field = _field(key, spec)
        if field is not None:
            common, advanced = _split([field])
            blocks.append({"title": "", "common": common, "advanced": advanced})
    return {
        "key": key,
        "title": str(spec.get("description") or key),
        "blocks": [block for block in blocks if block["common"] or block["advanced"]],
    }


def _blocks(prefix: str, spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = spec.get("items") if isinstance(spec.get("items"), Mapping) else {}
    own: list[dict[str, Any]] = []
    nested: list[dict[str, Any]] = []
    for name, child in items.items():
        path = f"{prefix}.{name}"
        if isinstance(child, Mapping) and (
            child.get("type") == "object" or (
                isinstance(child.get("items"), Mapping) and child.get("type") != "dict"
            )
        ):
            common, advanced = _split(_leaves(path, child))
            nested.append(
                {
                    "title": str(child.get("description") or name),
                    "common": common,
                    "advanced": advanced,
                }
            )
            continue
        field = _field(path, child if isinstance(child, Mapping) else {})
        if field is not None:
            own.append(field)
    common, advanced = _split(own)
    return [{"title": "", "common": common, "advanced": advanced}, *nested]


def _leaves(prefix: str, spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = spec.get("items") if isinstance(spec.get("items"), Mapping) else {}
    found: list[dict[str, Any]] = []
    for name, child in items.items():
        path = f"{prefix}.{name}"
        if isinstance(child, Mapping) and child.get("type") == "object":
            found.extend(_leaves(path, child))
            continue
        field = _field(path, child if isinstance(child, Mapping) else {})
        if field is not None:
            found.append(field)
    return found


def _split(fields: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    common = [field for field in fields if field["level"] == "common"]
    advanced = [field for field in fields if field["level"] != "common"]
    return common, advanced


def _field(path: str, spec: Mapping[str, Any]) -> dict[str, Any] | None:
    if spec.get("invisible"):
        return None
    widget = _widget(path, spec)
    if widget is None:
        return None
    options = []
    if widget == "select":
        options = [{"value": str(item), "label": str(item)} for item in spec.get("options") or []]
    elif widget == "ugc":
        options = [{"value": str(value), "label": label} for value, label in UGC_OPTIONS]
    elif widget == "thinking":
        options = [
            {"value": mode, "label": mode or "跟随 text.thinking"} for mode in THINKING_MODES
        ]
    slider = spec.get("slider") if isinstance(spec.get("slider"), Mapping) else None
    return {
        "path": path,
        "label": str(spec.get("description") or path),
        "hint": str(spec.get("hint") or ""),
        "note": SETTINGS_NOTE if path in NOTE_PATHS else "",
        "widget": widget,
        "level": _level(path, widget),
        "options": options,
        "slider": dict(slider) if slider else None,
        "default": copy.deepcopy(spec.get("default")),
        "number_type": spec.get("type") if widget == "number" else None,
    }


def _widget(path: str, spec: Mapping[str, Any]) -> str | None:
    if path == "text.thinking_by_task":
        return "thinking"
    if path == "interaction.tts_provider_ids":
        return "tts"
    if path == "interaction.qzone.ugc_right":
        return "ugc"
    if spec.get("secret"):
        return "secret"
    if spec.get("_special") == "select_provider":
        return "provider"
    if spec.get("options"):
        return "select"
    kind = spec.get("type")
    if kind == "bool":
        return "bool"
    if kind == "text":
        return "textarea"
    if kind in {"int", "float"}:
        return "number"
    if kind == "dict":
        return "dict_rows"
    if kind == "object":
        return None
    return "text"


def _level(path: str, widget: str) -> str:
    if path in ADVANCED_PATHS or widget in {"thinking", "tts", "dict_rows"}:
        return "advanced"
    if widget in {"bool", "select", "textarea", "secret", "provider", "text", "ugc"}:
        return "common"
    if path in KNOBS:
        return "common"
    return "advanced"


def _iter_fields(schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for group in describe(schema):
        if group["kind"] != "sections":
            continue
        for section in group["sections"]:
            for block in section["blocks"]:
                found.extend(block["common"])
                found.extend(block["advanced"])
    return found


def _override_fields(schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for key, path in OVERRIDE_PATHS.items():
        spec = _lookup(schema, path) or {}
        widget = _widget(path, spec)
        if widget == "bool":
            kind = "tri"
        elif widget == "provider" or key == "tts_provider_id":
            kind = "provider"
        elif widget == "ugc":
            kind = "ugc"
        elif widget == "select":
            kind = "select"
        elif widget == "textarea":
            kind = "textarea"
        elif widget == "number":
            kind = "number"
        else:
            kind = "text"
        options = []
        if kind == "select":
            options = [{"value": "", "label": "跟随全局"}]
            options.extend(
                {"value": str(item), "label": str(item)} for item in spec.get("options") or []
            )
        elif kind == "ugc":
            options = [{"value": "", "label": "跟随全局"}]
            options.extend({"value": str(value), "label": label} for value, label in UGC_OPTIONS)
        elif kind == "tri":
            options = [
                {"value": "", "label": "跟随全局"},
                {"value": "true", "label": "开"},
                {"value": "false", "label": "关"},
            ]
        slider = spec.get("slider") if isinstance(spec.get("slider"), Mapping) else None
        fields.append(
            {
                "key": key,
                "path": path,
                "label": str(spec.get("description") or key),
                "hint": str(spec.get("hint") or ""),
                "widget": kind,
                "options": options,
                "slider": dict(slider) if slider else None,
                "number_type": spec.get("type") if kind == "number" else None,
            }
        )
    return fields


def _lookup(schema: Mapping[str, Any], path: str) -> Mapping[str, Any] | None:
    node: Any = schema
    for part in path.split("."):
        if not isinstance(node, Mapping):
            return None
        items = node.get("items")
        if isinstance(items, Mapping) and part in items:
            node = items[part]
            continue
        if part in node and isinstance(node.get(part), Mapping) and part != "items":
            node = node[part]
            continue
        return None
    return node if isinstance(node, Mapping) else None


def _apply_value(result: dict[str, Any], field: Mapping[str, Any], value: Any, errors: list[str]) -> None:
    label = str(field["label"])
    widget = field["widget"]
    try:
        parsed = _coerce(field, value)
    except ValueError:
        if widget == "number":
            errors.append(f"{label}：请填一个数字")
        elif widget == "bool":
            errors.append(f"{label}：请选择开或关")
        else:
            errors.append(f"{label}：不是可选项")
        return
    _set(result, str(field["path"]), parsed)


def _coerce(field: Mapping[str, Any], value: Any) -> Any:
    widget = field["widget"]
    if widget == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError(widget)
    if widget == "number":
        number = _parse_number(value, as_int=field.get("number_type") != "float")
        return _clamp(number, field.get("slider"), as_int=field.get("number_type") != "float")
    if widget == "ugc":
        number = _parse_number(value, as_int=True)
        allowed = {item for item, _label in UGC_OPTIONS}
        if number not in allowed:
            raise ValueError(widget)
        return number
    if widget == "select":
        text = str(value if value is not None else "")
        allowed = {item["value"] for item in field.get("options") or []}
        if text not in allowed:
            raise ValueError(widget)
        return text
    if widget in {"text", "textarea", "provider"}:
        return "" if value is None else str(value)
    raise ValueError(widget)


def _apply_secret(result: dict[str, Any], path: str, incoming: Any) -> None:
    if not isinstance(incoming, Mapping):
        return
    if incoming.get("clear"):
        _set(result, path, "")
        return
    text = str(incoming.get("value") or "")
    if text.strip():
        _set(result, path, text)


def _apply_thinking(
    result: dict[str, Any], path: str, incoming: Any, label: str, errors: list[str]
) -> None:
    if not isinstance(incoming, Mapping):
        errors.append(f"{label}：格式不对")
        return
    current = _get(result, path, {})
    merged = dict(current) if isinstance(current, Mapping) else {}
    failed = False
    for task in THINKING_TASKS:
        if task not in incoming:
            continue
        mode = "" if incoming.get(task) is None else str(incoming.get(task))
        if mode not in THINKING_MODES:
            errors.append(f"{label}：{task} 不是可选项")
            failed = True
            continue
        merged[task] = mode
    if not failed:
        _set(result, path, merged)


def _apply_tts(
    result: dict[str, Any], path: str, incoming: Any, label: str, errors: list[str]
) -> None:
    if not isinstance(incoming, Mapping):
        errors.append(f"{label}：格式不对")
        return
    rows: dict[str, str] = {}
    for bot, provider in incoming.items():
        bot_id = str(bot or "").strip()
        provider_id = str(provider or "").strip()
        if not bot_id or not provider_id:
            continue
        rows[bot_id] = provider_id
    _set(result, path, rows)


def _apply_dict(
    result: dict[str, Any], path: str, incoming: Any, label: str, errors: list[str]
) -> None:
    if not isinstance(incoming, Mapping):
        errors.append(f"{label}：格式不对")
        return
    rows: dict[str, str] = {}
    for key, value in incoming.items():
        name = str(key or "").strip()
        if not name:
            continue
        rows[name] = "" if value is None else str(value)
    _set(result, path, rows)


def _apply_overrides(
    schema: Mapping[str, Any], result: dict[str, Any], incoming: Any, errors: list[str]
) -> None:
    if incoming is None:
        return
    if not isinstance(incoming, Mapping):
        errors.append("按机器人覆盖：格式不对")
        return
    fields = {field["key"]: field for field in _override_fields(schema)}
    current = result.get("bot_overrides")
    current_bots = dict(current) if isinstance(current, Mapping) else {}
    merged: dict[str, dict[str, Any]] = {}
    for bot, data in current_bots.items():
        if str(bot) not in incoming and isinstance(data, Mapping):
            merged[str(bot)] = dict(data)
    for bot, data in incoming.items():
        bot_id = str(bot or "").strip()
        if not bot_id:
            continue
        if not isinstance(data, Mapping):
            errors.append(f"按机器人覆盖：{bot_id} 格式不对")
            continue
        existing = current_bots.get(bot_id)
        if existing is None:
            existing = current_bots.get(bot)
        base = dict(existing) if isinstance(existing, Mapping) else {}
        for key, field in fields.items():
            if key not in data:
                continue
            value = data.get(key)
            if _blank(value):
                base.pop(key, None)
                continue
            try:
                base[key] = _coerce_override(field, value)
            except ValueError:
                errors.append(f"{bot_id} 的{field['label']}：没有通过校验")
        if base:
            merged[bot_id] = base
    result["bot_overrides"] = merged


def _coerce_override(field: Mapping[str, Any], value: Any) -> Any:
    widget = field["widget"]
    if widget == "tri":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "开"}:
            return True
        if text in {"false", "0", "no", "关"}:
            return False
        raise ValueError(widget)
    if widget == "number":
        number = _parse_number(value, as_int=field.get("number_type") != "float")
        return _clamp(number, field.get("slider"), as_int=field.get("number_type") != "float")
    if widget == "ugc":
        number = _parse_number(value, as_int=True)
        if number not in {item for item, _label in UGC_OPTIONS}:
            raise ValueError(widget)
        return number
    if widget == "select":
        text = str(value)
        allowed = {item["value"] for item in field.get("options") or [] if item["value"] != ""}
        if text not in allowed:
            raise ValueError(widget)
        return text
    return "" if value is None else str(value)


def _present_thinking(value: Any, default: Any) -> dict[str, str]:
    current = dict(value) if isinstance(value, Mapping) else {}
    fallback = dict(default) if isinstance(default, Mapping) else {}
    shown: dict[str, str] = {}
    for task in THINKING_TASKS:
        raw = current.get(task, fallback.get(task, ""))
        shown[task] = "" if raw is None else str(raw)
    return shown


def _present_tts(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(bot): str(provider)
        for bot, provider in value.items()
        if str(bot or "").strip() and str(provider or "").strip()
    }


def _present_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): "" if item is None else str(item) for key, item in value.items()}


def _parse_number(value: Any, *, as_int: bool) -> int | float:
    if isinstance(value, bool) or value is None:
        raise ValueError("number")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("number")
        value = float(text)
    if not isinstance(value, (int, float)):
        raise ValueError("number")
    if as_int:
        if isinstance(value, float) and not float(value).is_integer():
            raise ValueError("number")
        return int(value)
    return float(value)


def _clamp(number: int | float, slider: Any, *, as_int: bool) -> int | float:
    if isinstance(slider, Mapping):
        lo = slider.get("min")
        hi = slider.get("max")
        if isinstance(lo, (int, float)) and not isinstance(lo, bool):
            number = max(number, lo)
        if isinstance(hi, (int, float)) and not isinstance(hi, bool):
            number = min(number, hi)
    if as_int:
        return int(number)
    return float(number)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
    if value in (1, 0):
        return bool(value)
    return default


def _as_ugc(value: Any, default: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        try:
            number = int(default)
        except (TypeError, ValueError):
            return 1
    return number


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _get(data: Mapping[str, Any], path: str, default: Any = None) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


def _set(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value
