"""生图提供商协议适配器单测：OpenAI Images（generations/edits）、Gemini、OpenAI Chat。"""

from __future__ import annotations

import base64
import logging
from typing import Any

import pytest

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.core.http import HTTPResult
from astrbot_plugin_shadow_memory.imaging.providers import (
    ImageProviderClient,
    ProviderError,
    RefImageInput,
    gemini_generate_url,
    openai_images_url,
    pick_provider,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32


class FakeHTTP:
    def __init__(self) -> None:
        self.json_calls: list[dict[str, Any]] = []
        self.multipart_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.next_json: HTTPResult = HTTPResult(status=200, body={})
        self.next_multipart: HTTPResult = HTTPResult(status=200, body={})
        self.next_get: HTTPResult = HTTPResult(status=200, body=JPG_BYTES)

    async def post_json(self, url, payload, headers=None, timeout_s=5.0):
        self.json_calls.append({"url": url, "payload": payload, "headers": headers or {}})
        return self.next_json

    async def post_multipart(self, url, data, files, headers=None, timeout_s=60.0):
        self.multipart_calls.append(
            {"url": url, "data": data, "files": files, "headers": headers or {}}
        )
        return self.next_multipart

    async def get_bytes(self, url, *, max_bytes, timeout_s=10.0, max_redirects=3, validate=None):
        self.get_calls.append(url)
        return self.next_get


def _client(http: FakeHTTP) -> ImageProviderClient:
    return ImageProviderClient(
        http, Audit(logging.getLogger("test")), url_validator=lambda url: None
    )


def test_url_helpers_and_pick_provider():
    assert openai_images_url("https://api.openai.com", edit=False) == "https://api.openai.com/v1/images/generations"
    assert openai_images_url("https://api.openai.com/v1", edit=True) == "https://api.openai.com/v1/images/edits"
    assert gemini_generate_url("https://generativelanguage.googleapis.com", "gemini-2.5-flash-image") == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent"
    )
    providers = [
        {"id": "p1", "protocol": "openai_images", "base_url": "https://a.example/v1", "model": "gpt-image-1"},
        {"id": "p2", "protocol": "gemini", "base_url": "https://b.example", "model": "gemini-2.5-flash-image"},
    ]
    assert pick_provider(providers, "")["id"] == "p1"
    assert pick_provider(providers, "p2")["id"] == "p2"
    assert pick_provider(providers, "missing") is None


async def test_openai_images_generations_and_edits():
    http = FakeHTTP()
    b64 = base64.b64encode(PNG_BYTES).decode("ascii")
    http.next_json = HTTPResult(
        status=200, body={"data": [{"b64_json": b64, "revised_prompt": "revised"}]}
    )
    client = _client(http)
    provider = {
        "id": "oa",
        "protocol": "openai_images",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test",
        "model": "gpt-image-1",
        "size": "1024x1024",
    }

    img = await client.generate(provider, "a cup of latte")
    assert img.data == PNG_BYTES
    assert img.mime == "image/png"
    assert img.revised_prompt == "revised"
    assert http.json_calls[0]["url"].endswith("/images/generations")
    assert http.json_calls[0]["headers"]["Authorization"] == "Bearer sk-test"

    # 带多张参考图时自动切换 /images/edits multipart
    http.next_multipart = HTTPResult(
        status=200, body={"data": [{"url": "https://cdn.example/out.jpg"}]}
    )
    refs = [
        RefImageInput("style", "水彩", "", PNG_BYTES, "image/png", "style.png"),
        RefImageInput("self", "本人", "", JPG_BYTES, "image/jpeg", "self.jpg"),
    ]
    img2 = await client.generate(provider, "girl holding latte", refs)
    assert img2.data == JPG_BYTES
    assert img2.mime == "image/jpeg"
    assert http.multipart_calls[0]["url"].endswith("/images/edits")
    assert [f[0] for f in http.multipart_calls[0]["files"]] == ["image[]", "image[]"]
    edits_prompt = http.multipart_calls[0]["data"]["prompt"]
    assert "[Reference Image Roles]" in edits_prompt
    assert "Image 1:" in edits_prompt and "ART STYLE REFERENCE ONLY" in edits_prompt
    assert "Do NOT copy" in edits_prompt
    assert "Image 2:" in edits_prompt and "CHARACTER IDENTITY REFERENCE ONLY" in edits_prompt
    assert "Do NOT copy the outfit" in edits_prompt
    assert "Do NOT copy this image's lineart or coloring style" in edits_prompt
    assert "girl holding latte" in edits_prompt
    assert http.get_calls == ["https://cdn.example/out.jpg"]


async def test_gemini_native_with_refs():
    http = FakeHTTP()
    b64 = base64.b64encode(PNG_BYTES).decode("ascii")
    http.next_json = HTTPResult(
        status=200,
        body={
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "done"},
                            {"inlineData": {"mimeType": "image/png", "data": b64}},
                        ]
                    }
                }
            ]
        },
    )
    client = _client(http)
    provider = {
        "id": "gem",
        "protocol": "gemini",
        "base_url": "https://generativelanguage.googleapis.com",
        "api_key": "gk-test",
        "model": "gemini-2.5-flash-image",
        "aspect_ratio": "3:4",
    }
    refs = [RefImageInput("style", "胶片风", "暖色调", PNG_BYTES, "image/png", "film.png")]
    img = await client.generate(provider, "window view at dusk", refs)
    assert img.data == PNG_BYTES
    call = http.json_calls[0]
    assert call["headers"]["x-goog-api-key"] == "gk-test"
    assert call["payload"]["generationConfig"]["imageConfig"]["aspectRatio"] == "3:4"
    parts = call["payload"]["contents"][0]["parts"]
    assert any("inlineData" in p for p in parts)
    text_parts = [p["text"] for p in parts if "text" in p]
    assert any(
        "ART STYLE REFERENCE ONLY" in t and "do NOT copy" in t and "edge softness" in t
        for t in text_parts
    )
    assert "do NOT default to photorealism" in text_parts[-1]


async def test_openai_chat_base64_and_markdown_url():
    http = FakeHTTP()
    b64 = base64.b64encode(PNG_BYTES).decode("ascii")
    http.next_json = HTTPResult(
        status=200,
        body={
            "choices": [
                {"message": {"content": f"好的 ![img](data:image/png;base64,{b64})"}}
            ]
        },
    )
    client = _client(http)
    provider = {
        "id": "relay",
        "protocol": "openai_chat",
        "base_url": "https://relay.example/v1",
        "api_key": "sk-r",
        "model": "gemini-2.5-flash-image",
    }
    img = await client.generate(
        provider,
        "cat on sofa",
        [
            RefImageInput("style", "二次元赛璐璐", "", PNG_BYTES, "image/png", "anime.png"),
            RefImageInput("item", "橘猫", "短毛", JPG_BYTES, "image/jpeg", "cat.jpg"),
        ],
    )
    assert img.data == PNG_BYTES
    chat_texts = [
        p["text"]
        for p in http.json_calls[0]["payload"]["messages"][0]["content"]
        if p.get("type") == "text"
    ]
    assert any("ART STYLE REFERENCE ONLY" in t for t in chat_texts)
    assert any("SUBJECT REFERENCE" in t for t in chat_texts)
    assert "do NOT default to photorealism" in chat_texts[-1]

    http.next_json = HTTPResult(
        status=200,
        body={"choices": [{"message": {"content": "![photo](https://img.example/cat.jpg)"}}]},
    )
    img2 = await client.generate(provider, "cat on sofa")
    assert img2.data == JPG_BYTES

    http.next_json = HTTPResult(status=500, body={"error": {"message": "upstream boom"}})
    with pytest.raises(ProviderError, match="upstream boom"):
        await client.generate(provider, "fail")
