"""契约测试：直接读本机 AstrBot 源码，断言插件依赖的接口仍然存在。

AstrBot 源码路径可用环境变量 ``ASTRBOT_SRC`` 覆盖。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

SRC = Path(
    os.environ.get(
        "ASTRBOT_SRC", r"D:\projects\other\Astrbot\origin_code\AstrBot-master"
    )
)
pytestmark = pytest.mark.skipif(
    not (SRC / "astrbot").exists(), reason=f"AstrBot source not found at {SRC}"
)

PLUGIN = Path(__file__).resolve().parents[2]


def read(relative: str) -> str:
    return (SRC / relative).read_text(encoding="utf-8", errors="replace")


def test_version_is_at_least_4_29_3():
    text = read("pyproject.toml")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml 里没有 version"
    parts = [int(value) for value in match.group(1).split(".")[:3]]
    assert parts >= [4, 29, 3]


def test_plugin_loader_contract():
    text = read("astrbot/core/star/star_manager.py")
    assert "def _get_modules" in text
    assert '"main.py"' in text
    assert "data.plugins." in text
    assert "_conf_schema.json" in text
    assert "def _load_plugin_i18n" in text


def test_star_base_and_lifecycle_contract():
    text = read("astrbot/core/star/base.py")
    assert "class Star(" in text
    assert "def __init_subclass__" in text
    assert "async def initialize" in text
    assert "async def terminate" in text


def test_handler_decorators_exist():
    text = read("astrbot/core/star/register/star_handler.py")
    for name in (
        "register_command",
        "register_event_message_type",
        "register_on_llm_request",
        "register_on_llm_response",
        "register_on_decorating_result",
        "register_after_message_sent",
        "register_on_astrbot_loaded",
        "register_on_platform_loaded",
        "register_llm_tool",
        "register_permission_type",
    ):
        assert f"def {name}" in text, name
    assert "docstring_parser" in text
    assert "PY_TO_JSON_TYPE" in text


def test_pipeline_gate_and_hooks():
    stage = read("astrbot/core/pipeline/process_stage/stage.py")
    assert "event.is_at_or_wake_command" in stage
    assert "event._has_send_oper" in stage
    assert "event.call_llm" in stage

    internal = read(
        "astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py"
    )
    assert "EventType.OnLLMRequestEvent" in internal
    assert "_no_save" in internal

    decorate = read("astrbot/core/pipeline/result_decorate/stage.py")
    assert "EventType.OnDecoratingResultEvent" in decorate

    waking = read("astrbot/core/pipeline/waking_check/stage.py")
    assert "EventType.AdapterMessageEvent" in waking
    assert "activated_handlers" in waking


def test_provider_request_and_content_parts():
    entities = read("astrbot/core/provider/entities.py")
    for field in ("prompt", "system_prompt", "contexts", "extra_user_content_parts"):
        assert field in entities
    assert "class TokenUsage" in entities
    assert "input_cached" in entities

    message = read("astrbot/core/agent/message.py")
    assert "def mark_as_temp" in message
    assert "class TextPart" in message


def test_cron_manager_contract():
    text = read("astrbot/core/cron/manager.py")
    assert "async def add_basic_job" in text
    assert "async def add_active_job" in text
    assert "run_once" in text and "run_at" in text
    assert "CronTrigger.from_crontab" in text
    assert "def delete_job" in text
    assert "def list_jobs" in text


def test_context_and_provider_lookup_contract():
    text = read("astrbot/core/star/context.py")
    for name in (
        "async def send_message",
        "async def get_using_provider_async",
        "async def get_using_tts_provider_async",
        "async def activate_llm_tool_async",
        "async def deactivate_llm_tool_async",
        "def get_provider_by_id",
        "def get_llm_tool_manager",
    ):
        assert name in text, name


def test_tts_provider_contract():
    text = read("astrbot/core/provider/provider.py")
    assert "class TTSProvider" in text
    assert "async def get_audio" in text


def test_plugin_data_dir_helper():
    text = read("astrbot/core/star/star_tools.py")
    assert "def get_data_dir" in text
    assert "plugin_data" in text


def test_aiocqhttp_event_exposes_bot():
    text = read("astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py")
    assert "self.bot = bot" in text
    assert "async def send(" in text

    adapter = read(
        "astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py"
    )
    assert 'name="aiocqhttp"' in adapter


def test_message_type_enum():
    text = read("astrbot/core/platform/message_type.py")
    assert "GROUP_MESSAGE" in text and "FRIEND_MESSAGE" in text


def test_system_one_decision_contract():
    """AstrBot 自带的 System One provider 可以作为 score 语义的参照。"""
    text = read("astrbot/core/provider/sources/system_one_decision_source.py")
    assert "class SystemOneDecisionProvider" in text
    assert '"/systemone"' in text
    assert "0 <= score <= len(question.criteria) - 1" in text
    assert "probabilities" in text


def test_plugin_source_uses_expected_hooks():
    main_text = (PLUGIN / "main.py").read_text(encoding="utf-8")
    qzone_text = (PLUGIN / "interaction" / "qzone.py").read_text(encoding="utf-8")
    for snippet in (
        "@filter.event_message_type(filter.EventMessageType.ALL",
        "@filter.on_llm_request()",
        "@filter.on_llm_response()",
        "@filter.on_decorating_result()",
        "@filter.after_message_sent()",
        "@filter.on_astrbot_loaded()",
        "@filter.on_platform_loaded()",
        "event.is_at_or_wake_command = True",
        ".mark_as_temp()",
        "result.chain.clear()",
        "activate_llm_tool_async(name)",
        "deactivate_llm_tool_async(name)",
        '"media": config.bool("interaction.media.enabled"',
        '"send_image": config.bool("interaction.send_image.enabled"',
        "Image.fromBytes(",
        "describe_image",
        "DecisionBooleanQuestion",
        "_build_decision_question",
        "decision.provider_id",
        "restore_full",
        "_read_backup",
        "register_web_api",
        "_inject_context",
        "session_buffer.record",
        "resolve_tts_provider_id",
        "_inject_environment",
        "build_environment_prompt",
        "environment.prompt",
        "environment_injected",
        "persona_inject_start",
        "provider_tool_use_disabled",
        "say_example_messages",
        "say_example_injected",
        "MEMORY_OWNERSHIP_PROMPT",
        "format_recall_block",
        "recall_inject",
        "set_debug",
        "logging.debug",
        "event/rebuild",
        "web_event_rebuild",
        "rebuild_event",
    ):
        assert snippet in main_text, snippet
    db_text = (PLUGIN / "core" / "db.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS session_messages" in db_text
    assert "bot.call_action" in qzone_text
    config_text = (PLUGIN / "core" / "config.py").read_text(encoding="utf-8")
    assert "default_image_caption_provider_id" in config_text
    view = PLUGIN / "views" / "memory" / "index.html"
    assert view.is_file()
    assert (view.parent / "app.js").is_file()
    assert "AstrBotPluginView" in (view.parent / "app.js").read_text(encoding="utf-8")


def test_astrbot_plugin_page_contract():
    service = read("astrbot/dashboard/services/plugin_page_service.py")
    assert 'PLUGIN_PAGE_ROOT_DIR_NAMES = ("views", "pages")' in service
    assert 'PLUGIN_PAGE_ENTRY_FILE_NAME = "index.html"' in service
    assert "/api/plugin/page/bridge-sdk.js" in service
    bridge = read("astrbot/dashboard/plugin_page_bridge.js")
    assert "window.AstrBotPluginView" in bridge
    assert "apiGet(endpoint, params)" in bridge
    assert "apiPost(endpoint, body)" in bridge
    assert "download(endpoint, params, filename)" in bridge
    docs = read("docs/zh/dev/star/guides/plugin-pages.md")
    assert "context.register_web_api" in docs


def test_astrbot_decision_provider_contract():
    provider = read("astrbot/core/provider/provider.py")
    assert "class DecisionProvider" in provider
    assert "async def ask" in provider
    entities = read("astrbot/core/provider/entities.py")
    for name in (
        "DecisionBooleanQuestion",
        "DecisionChoiceQuestion",
        "DecisionScoreQuestion",
        "DecisionBooleanAnswer",
        "DecisionChoiceAnswer",
        "DecisionScoreAnswer",
    ):
        assert f"class {name}" in entities, name
    api = read("astrbot/api/provider/__init__.py")
    assert "DecisionBooleanQuestion" in api
    assert "DecisionScoreAnswer" in api


def test_astrbot_vision_and_modality_contract():
    agent = read("astrbot/core/astr_main_agent.py")
    assert "def _provider_supports_modality" in agent
    assert "default_image_caption_provider_id" in agent
    image_input = read("astrbot/core/utils/image_input.py")
    assert "supports_image" in image_input
    provider_cfg = read("astrbot/core/config/default.py")
    assert '"modalities"' in provider_cfg
    provider = read("astrbot/core/provider/provider.py")
    assert "image_urls" in provider


def test_memory_modules_do_not_import_astrbot():
    offenders: list[str] = []
    for path in PLUGIN.rglob("*.py"):
        if path.name == "main.py" or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(from|import)\s+astrbot", text, re.MULTILINE):
            offenders.append(str(path.relative_to(PLUGIN)))
    assert offenders == []
