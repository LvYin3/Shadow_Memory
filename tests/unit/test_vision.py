from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.core.config import ConfigView, pick_vision_provider_id
from astrbot_plugin_shadow_memory.core.decision import chat_url
from astrbot_plugin_shadow_memory.core.http import HTTPResult
from astrbot_plugin_shadow_memory.core.textgen import TextClient

from ..fakes import FakeHTTP

PNG = b"\x89PNG\r\n\x1a\n" + b"png-body" * 8


class FakeVisionProvider:
    def __init__(self, text="海边黄昏，风很大", fail=False, delay=0.0):
        self.text = text
        self.fail = fail
        self.delay = delay
        self.calls: list[dict] = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("no vision")
        return SimpleNamespace(completion_text=self.text)


def make_client(raw=None, provider=None, http=None):
    cfg = ConfigView(raw or {"text": {"base_url": ""}}, "")
    http = http or FakeHTTP()

    async def resolver(bot_id, umo):
        return provider

    return TextClient(
        cfg,
        http,
        Audit(),
        provider_resolver=resolver,
        vision_resolver=resolver,
    )


def test_pick_vision_provider_id_priority():
    cfg = ConfigView(
        {"vision": {"provider_id": "vision-1"}, "text": {"provider_id": "text-1"}}, ""
    )
    assert pick_vision_provider_id(cfg, {"default_image_caption_provider_id": "cap-1"}) == "vision-1"

    cfg2 = ConfigView({"vision": {"provider_id": ""}, "text": {"provider_id": "text-1"}}, "")
    assert pick_vision_provider_id(cfg2, {"default_image_caption_provider_id": "cap-1"}) == "cap-1"

    cfg3 = ConfigView({"vision": {"provider_id": ""}, "text": {"provider_id": "text-1"}}, "")
    assert pick_vision_provider_id(cfg3, {}) == "text-1"
    assert pick_vision_provider_id(cfg3, None) == "text-1"


async def test_describe_image_via_provider_uses_data_uri():
    provider = FakeVisionProvider()
    client = make_client(provider=provider)
    caption = await client.describe_image(PNG, mime="image/png", prompt="描述这张图")
    assert caption == "海边黄昏，风很大"
    sent = provider.calls[0]
    assert sent["image_urls"][0].startswith("data:image/png;base64,")
    assert sent["prompt"] == "描述这张图"


async def test_describe_image_truncates_to_max_chars():
    provider = FakeVisionProvider(text="一二三四五六七八九十")
    client = make_client(provider=provider)
    caption = await client.describe_image(PNG, mime="image/png", prompt="x", max_chars=4)
    assert caption == "一二三四"


async def test_describe_image_strips_quotes_and_extra_lines():
    provider = FakeVisionProvider(text='“海边日落”\n第二行不要')
    client = make_client(provider=provider)
    caption = await client.describe_image(PNG, mime="image/png", prompt="x")
    assert caption == "海边日落"


async def test_describe_image_provider_failure_returns_none():
    client = make_client(provider=FakeVisionProvider(fail=True))
    assert await client.describe_image(PNG, mime="image/png", prompt="x") is None


async def test_describe_image_timeout_returns_none():
    client = make_client(provider=FakeVisionProvider(delay=1.0))
    caption = await client.describe_image(PNG, mime="image/png", prompt="x", timeout_s=0.01)
    assert caption is None


async def test_describe_image_direct_path():
    http = FakeHTTP()
    url = chat_url("https://api.deepseek.com/v1")
    http.set(
        url,
        HTTPResult(
            status=200,
            body={"choices": [{"message": {"content": "一张海边的照片"}}]},
        ),
    )
    cfg = ConfigView(
        {
            "text": {
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-test",
                "model": "deepseek-chat",
            }
        },
        "",
    )

    async def no_provider(bot_id, umo):
        return None

    client = TextClient(
        cfg, http, Audit(), provider_resolver=no_provider, vision_resolver=no_provider
    )
    caption = await client.describe_image(
        PNG, mime="image/png", prompt="描述", model="deepseek-flash"
    )
    assert caption == "一张海边的照片"
    payload = http.calls[0][1]
    assert payload["model"] == "deepseek-flash"
    blocks = payload["messages"][0]["content"]
    assert blocks[0]["type"] == "text"
    assert blocks[1]["type"] == "image_url"
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_tokens"] == 800


async def test_describe_image_direct_retries_without_thinking_on_400():
    http = FakeHTTP()
    url = chat_url("https://example.com/v1")
    payloads: list[dict] = []

    def responder(payload):
        payloads.append(dict(payload))
        if len(payloads) == 1:
            return HTTPResult(status=400, body={"error": "unknown field thinking"})
        return HTTPResult(status=200, body={"choices": [{"message": {"content": "重试成功"}}]})

    http.set(url, responder)
    cfg = ConfigView(
        {"text": {"base_url": "https://example.com/v1", "model": "deepseek-flash"}}, ""
    )

    async def no_provider(bot_id, umo):
        return None

    client = TextClient(
        cfg, http, Audit(), provider_resolver=no_provider, vision_resolver=no_provider
    )
    caption = await client.describe_image(PNG, mime="image/png", prompt="x")
    assert caption == "重试成功"
    assert "thinking" in payloads[0]
    assert "thinking" not in payloads[1]


async def test_describe_image_direct_retries_with_more_tokens_on_length():
    http = FakeHTTP()
    url = chat_url("https://example.com/v1")
    payloads: list[dict] = []

    def responder(payload):
        payloads.append(dict(payload))
        if len(payloads) == 1:
            return HTTPResult(
                status=200,
                body={
                    "choices": [
                        {"message": {"content": ""}, "finish_reason": "length"}
                    ]
                },
            )
        return HTTPResult(status=200, body={"choices": [{"message": {"content": "预算翻倍后成功"}}]})

    http.set(url, responder)
    cfg = ConfigView(
        {"text": {"base_url": "https://example.com/v1", "model": "deepseek-flash"}}, ""
    )

    async def no_provider(bot_id, umo):
        return None

    client = TextClient(
        cfg, http, Audit(), provider_resolver=no_provider, vision_resolver=no_provider
    )
    caption = await client.describe_image(
        PNG, mime="image/png", prompt="x", max_tokens=800
    )
    assert caption == "预算翻倍后成功"
    assert payloads[1]["max_tokens"] == 1600


async def test_describe_image_direct_failure_returns_none():
    http = FakeHTTP()  # 默认 500
    cfg = ConfigView({"text": {"base_url": "https://example.com/v1"}}, "")

    async def no_provider(bot_id, umo):
        return None

    client = TextClient(
        cfg, http, Audit(), provider_resolver=no_provider, vision_resolver=no_provider
    )
    assert await client.describe_image(PNG, mime="image/png", prompt="x") is None


async def test_describe_image_falls_back_to_direct_when_provider_fails():
    provider = FakeVisionProvider(fail=True)
    http = FakeHTTP()
    url = chat_url("https://example.com/v1")
    http.set(url, HTTPResult(status=200, body={"choices": [{"message": {"content": "兜底描述"}}]}))
    cfg = ConfigView(
        {"text": {"base_url": "https://example.com/v1", "model": "deepseek-flash"}}, ""
    )

    async def resolver(bot_id, umo):
        return provider

    client = TextClient(cfg, http, Audit(), provider_resolver=resolver, vision_resolver=resolver)
    assert await client.describe_image(PNG, mime="image/png", prompt="x") == "兜底描述"


async def test_describe_image_empty_data_returns_none():
    client = make_client(provider=FakeVisionProvider())
    assert await client.describe_image(b"", mime="image/png", prompt="x") is None


@pytest.mark.parametrize("text", ["", "   "])
async def test_describe_image_empty_answer_returns_none(text):
    client = make_client(provider=FakeVisionProvider(text=text))
    assert await client.describe_image(PNG, mime="image/png", prompt="x") is None
