"""插件元数据、配置 schema、模块可导入性。"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]


def test_metadata_has_required_fields():
    lines = (PLUGIN / "metadata.yaml").read_text(encoding="utf-8").splitlines()
    data = {}
    for line in lines:
        if ":" in line and not line.strip().startswith("#"):
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip().strip('"')
    for field in ("name", "desc", "author", "version"):
        assert data.get(field), f"metadata.yaml 缺少 {field}"
    assert data["name"] == "shadow_memory"
    assert data["author"] == "LvYin3"


def test_conf_schema_is_valid_json_and_well_formed():
    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    assert isinstance(schema, dict) and schema
    for key, item in schema.items():
        assert "type" in item, key
        assert item["type"] in {
            "string",
            "text",
            "int",
            "float",
            "bool",
            "object",
            "list",
            "dict",
            "file",
            "template_list",
        }, key
        if item["type"] == "object":
            assert "items" in item, f"{key} 是 object 但没有 items"
            for child_key, child in item["items"].items():
                assert "type" in child, f"{key}.{child_key}"
                if child["type"] == "object":
                    assert "items" in child, f"{key}.{child_key}"


def test_conf_schema_defaults_match_types():
    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))

    def walk(node: dict, prefix: str = ""):
        for key, item in node.items():
            path = f"{prefix}{key}"
            if item.get("type") == "object":
                walk(item["items"], path + ".")
                continue
            if "default" not in item:
                continue
            kind = item["type"]
            default = item["default"]
            if kind in {"string", "text"}:
                assert isinstance(default, str), path
            elif kind == "int":
                assert isinstance(default, int) and not isinstance(default, bool), path
            elif kind == "float":
                assert isinstance(default, (int, float)) and not isinstance(default, bool), path
            elif kind == "bool":
                assert isinstance(default, bool), path
            elif kind in {"list", "file", "template_list"}:
                assert isinstance(default, list), path
            elif kind in {"object", "dict"}:
                assert isinstance(default, dict), path

    walk(schema)


def test_requirements_do_not_pin_extra_packages():
    text = (PLUGIN / "requirements.txt").read_text(encoding="utf-8")
    for line in text.splitlines():
        assert line.strip().startswith("#") or not line.strip()


def test_all_framework_free_modules_import():
    modules = [
        "core.audit",
        "core.config",
        "core.db",
        "core.decision",
        "core.textgen",
        "core.http",
        "core.jsonx",
        "core.types",
        "core.locks",
        "core.kv",
        "memory.dynamics",
        "memory.edges",
        "memory.prompts",
        "memory.segmenter",
        "memory.persona",
        "memory.insights",
        "memory.facts",
        "memory.recall",
        "memory.recall_format",
        "memory.distill",
        "memory.consolidate",
        "memory.decay",
        "memory.seed",
        "memory.service",
        "memory.transfer",
        "interaction.gate",
        "interaction.sender",
        "interaction.result",
        "interaction.commands",
        "interaction.sessions",
        "interaction.notes",
        "interaction.memos",
        "interaction.media_dir",
        "interaction.media_fetch",
        "interaction.media",
        "interaction.media_transfer",
        "interaction.qzone",
        "interaction.context_inject",
        "interaction.environment",
        "interaction.session_buffer",
        "interaction.tts_binding",
        "webui.service",
    ]
    for name in modules:
        importlib.import_module(f"astrbot_plugin_shadow_memory.{name}")


def test_skill_files_exist():
    skill = PLUGIN / "skills" / "persona-seed"
    assert (skill / "SKILL.md").is_file()
    assert (skill / "references" / "schema.md").is_file()
    assert (skill / "examples.md").is_file()
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    assert "persona_seed/v1" in text
    assert "/memory import" in text
