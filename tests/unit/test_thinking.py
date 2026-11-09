from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.core.config import ConfigView, thinking_policy
from astrbot_plugin_shadow_memory.core.decision import chat_url
from astrbot_plugin_shadow_memory.core.http import HTTPResult
from astrbot_plugin_shadow_memory.core.textgen import TextClient

from ..fakes import FakeHTTP


class FakeProvider:
    def __init__(self, text="写好了"):
        self.text = text
        self.calls: list[dict] = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(completion_text=self.text)


def make_client(raw=None, provider=None, http=None):
    cfg = ConfigView(raw or {"text": {"base_url": ""}}, "")
    http = http or FakeHTTP()

    async def resolver(bot_id, umo):
        return provider

    client = TextClient(
        cfg, http, Audit(), provider_resolver=resolver, vision_resolver=resolver
    )
    client.http = http  # type: ignore[attr-defined]
    return client


def direct_raw(**overrides):
    data = {
        "text": {
            "base_url": "https://example.com/v1",
            "api_key": "sk-test",
            "model": "deepseek-flash",
        }
    }
    for key, value in overrides.items():
        data["text"][key] = value
    return data


def ok(text="写好了"):
    return HTTPResult(status=200, body={"choices": [{"message": {"content": text}}]})


# --------------------------------------------------------------------- 策略解析
def test_default_task_policies():
    cfg = ConfigView({}, "")
    assert thinking_policy(cfg, "fragment") == ("disabled", "")
    assert thinking_policy(cfg, "tension") == ("disabled", "")
    assert thinking_policy(cfg, "facts") == ("disabled", "")
    assert thinking_policy(cfg, "distill") == ("enabled", "")
    assert thinking_policy(cfg, "consolidate") == ("enabled", "")
    assert thinking_policy(cfg, "unknown") == ("auto", "")


def test_global_fallback_and_task_override():
    cfg = ConfigView(
        {
            "text": {
                "thinking": "disabled",
                "reasoning_effort": "high",
                "thinking_by_task": {"distill": "enabled", "fragment": ""},
            }
        },
        "",
    )
    assert thinking_policy(cfg, "distill") == ("enabled", "high")
    assert thinking_policy(cfg, "fragment") == ("disabled", "high")
    assert thinking_policy(cfg, "unknown") == ("disabled", "high")


def test_invalid_values_fall_back():
    cfg = ConfigView(
        {
            "text": {
                "thinking": "weird",
                "reasoning_effort": "ultra",
                "thinking_by_task": {
                    "fragment": "nope",
                    "tension": "",
                    "facts": "",
                    "distill": "",
                    "consolidate": "",
                },
            }
        },
        "",
    )
    assert thinking_policy(cfg, "fragment") == ("auto", "")
    assert thinking_policy(cfg, "distill") == ("auto", "")


def test_empty_task_uses_global():
    cfg = ConfigView({"text": {"thinking": "enabled", "reasoning_effort": "low"}}, "")
    assert thinking_policy(cfg, "") == ("enabled", "low")


# --------------------------------------------------------------------- 直连 payload
async def test_fragment_sends_thinking_disabled():
    http = FakeHTTP()
    http.set(chat_url("https://example.com/v1"), ok())
    client = make_client(direct_raw(), http=http)
    assert await client.generate("sys", "user", task="fragment") == "写好了"
    payload = http.calls[0][1]
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload


async def test_distill_sends_thinking_enabled_and_effort():
    http = FakeHTTP()
    http.set(chat_url("https://example.com/v1"), ok())
    client = make_client(direct_raw(reasoning_effort="high"), http=http)
    assert await client.generate("sys", "user", task="distill") == "写好了"
    payload = http.calls[0][1]
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    assert payload["model"] == "deepseek-flash"


async def test_auto_sends_no_thinking_fields():
    http = FakeHTTP()
    http.set(chat_url("https://example.com/v1"), ok())
    client = make_client(direct_raw(thinking="auto"), http=http)
    await client.generate("sys", "user", task="unknown")
    payload = http.calls[0][1]
    assert "thinking" not in payload
    assert "reasoning_effort" not in payload


async def test_direct_retries_without_thinking_on_400():
    http = FakeHTTP()
    url = chat_url("https://example.com/v1")
    payloads: list[dict] = []

    def responder(payload):
        payloads.append(dict(payload))
        if len(payloads) == 1:
            return HTTPResult(status=400, body={"error": "unknown field thinking"})
        return ok("去掉字段后成功")

    http.set(url, responder)
    client = make_client(direct_raw(), http=http)
    assert await client.generate("sys", "user", task="fragment") == "去掉字段后成功"
    assert "thinking" in payloads[0]
    assert "thinking" not in payloads[1]


async def test_generate_json_passes_task_to_direct_payload():
    http = FakeHTTP()
    http.set(chat_url("https://example.com/v1"), ok('{"fragment": "ok"}'))
    client = make_client(direct_raw(), http=http)
    result = await client.generate_json(
        "sys", "user", schema_hint='{"fragment": string}', task="facts"
    )
    assert result == {"fragment": "ok"}
    assert http.calls[0][1]["thinking"] == {"type": "disabled"}


# --------------------------------------------------------------------- provider 路径
async def test_provider_path_counts_skipped_thinking_once_warns():
    provider = FakeProvider("provider 写好了")
    client = make_client({"text": {"base_url": ""}}, provider=provider)
    assert await client.generate("sys", "user", task="fragment") == "provider 写好了"
    assert client.stats["thinking_skipped"] == 1
    assert client._warned_provider_thinking is True
    await client.generate("sys", "user", task="distill")
    assert client.stats["thinking_skipped"] == 2
    # provider 收到的参数里不该出现 thinking（AstrBot 会丢弃）
    assert "thinking" not in provider.calls[0]


async def test_provider_auto_does_not_count_skipped():
    provider = FakeProvider()
    client = make_client({"text": {"base_url": ""}}, provider=provider)
    await client.generate("sys", "user", task="unknown")
    assert client.stats["thinking_skipped"] == 0
