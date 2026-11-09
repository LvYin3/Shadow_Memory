"""写作用模型：碎片 / gist / 摘要 / 人格蒸馏 / 事实抽取。

两条来源：
1. AstrBot 已配置的对话提供商（推荐，配置 ``text.provider_id``，留空用会话默认）；
2. 直连 OpenAI 兼容站点（``text.base_url/api_key/model``），供脱离 AstrBot 的测试使用。
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, Awaitable, Callable, Protocol

from .audit import Audit
from .config import ConfigView, thinking_policy
from .decision import chat_url
from .http import HTTPClient
from .jsonx import extract_json


class TextUnavailable(Exception):
    """没有任何可用的写作模型。"""


class ChatProvider(Protocol):
    async def text_chat(self, **kwargs: Any) -> Any: ...


ProviderResolver = Callable[[str, str], Awaitable[ChatProvider | None]]


class TextClient:
    def __init__(
        self,
        config: ConfigView,
        http: HTTPClient,
        audit: Audit,
        provider_resolver: ProviderResolver | None = None,
        vision_resolver: ProviderResolver | None = None,
    ) -> None:
        self.config = config
        self.http = http
        self.audit = audit
        self._resolver = provider_resolver
        self._vision_resolver = vision_resolver
        self.provider_id = config.str("text.provider_id").strip()
        self.base_url = config.str("text.base_url").strip()
        self.api_key = config.str("text.api_key").strip()
        self.model = config.str("text.model", "deepseek-chat").strip() or "deepseek-chat"
        self.temperature = config.float("text.temperature", 0.4)
        self.max_tokens = config.int("text.max_tokens", 1200)
        self.stats = {"provider": 0, "direct": 0, "failures": 0, "thinking_skipped": 0}
        self._warned_provider_thinking = False

    @property
    def available(self) -> bool:
        return bool(self._resolver or self.base_url)

    async def generate(
        self,
        system: str,
        user: str,
        *,
        bot_id: str = "",
        umo: str = "",
        task: str = "",
    ) -> str | None:
        text = await self._via_provider(system, user, bot_id, umo, task)
        if text is not None:
            return text
        return await self._via_direct(system, user, task)

    async def generate_json(
        self,
        system: str,
        user: str,
        *,
        bot_id: str = "",
        umo: str = "",
        schema_hint: str = "",
        task: str = "",
    ) -> dict[str, Any] | None:
        sys_prompt = system
        if schema_hint:
            sys_prompt += (
                "\n\n输出格式：只返回一个 JSON 对象，不要 markdown 围栏，不要解释。\n"
                + schema_hint
            )
        raw = await self.generate(sys_prompt, user, bot_id=bot_id, umo=umo, task=task)
        if not raw:
            return None
        value = extract_json(raw)
        if isinstance(value, dict):
            return value
        self.stats["failures"] += 1
        self.audit.warn("写作模型输出不是 JSON 对象：%s", raw[:120])
        return None

    async def _via_provider(
        self, system: str, user: str, bot_id: str, umo: str, task: str = ""
    ) -> str | None:
        if self._resolver is None:
            return None
        mode, _ = thinking_policy(self.config, task)
        if mode != "auto":
            self.stats["thinking_skipped"] += 1
            if not self._warned_provider_thinking:
                self._warned_provider_thinking = True
                # 这是预期行为：AstrBot provider 不接受按调用的 thinking。
                # 只留一条 INFO 方便诊断，不再刷 WARN；配置说明见 README。
                self.audit.event(
                    "thinking_skipped",
                    task=task or "text",
                    mode=mode,
                    path="provider",
                )
        try:
            provider = await self._resolver(bot_id, umo)
        except Exception as exc:  # noqa: BLE001 - 解析器异常统一降级
            self.audit.warn("解析写作 provider 失败：%s", exc)
            return None
        if provider is None:
            return None
        try:
            response = await provider.text_chat(
                prompt=user,
                system_prompt=system,
                contexts=[],
            )
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("写作 provider 调用失败：%s", exc)
            return None
        text = getattr(response, "completion_text", None)
        if not text:
            self.stats["failures"] += 1
            return None
        self.stats["provider"] += 1
        return str(text)

    # ------------------------------------------------------------------ 视觉描述
    async def describe_image(
        self,
        data: bytes,
        *,
        mime: str,
        prompt: str,
        bot_id: str = "",
        umo: str = "",
        model: str = "",
        max_chars: int = 0,
        timeout_s: float = 20.0,
        max_tokens: int = 800,
        disable_thinking: bool = True,
    ) -> str | None:
        """用视觉模型描述一张图；失败返回 None（调用方降级）。"""
        if not data:
            return None
        encoded = base64.b64encode(data).decode("ascii")
        data_uri = f"data:{mime or 'image/png'};base64,{encoded}"
        text = await self._try_describe(
            self._describe_via_provider(data_uri, prompt, bot_id, umo), timeout_s
        )
        if not text:
            text = await self._try_describe(
                self._describe_direct(
                    data_uri,
                    prompt,
                    model,
                    max_tokens=max_tokens,
                    disable_thinking=disable_thinking,
                ),
                timeout_s,
            )
        return _clean_caption(text, max_chars)

    @staticmethod
    async def _try_describe(coro: Awaitable[str | None], timeout_s: float) -> str | None:
        try:
            return await asyncio.wait_for(coro, timeout=max(0.5, float(timeout_s)))
        except Exception:  # noqa: BLE001 - 超时/取消/网络异常统一降级
            return None

    async def _describe_via_provider(
        self, data_uri: str, prompt: str, bot_id: str, umo: str
    ) -> str | None:
        resolver = self._vision_resolver or self._resolver
        if resolver is None:
            return None
        try:
            provider = await resolver(bot_id, umo)
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("解析视觉 provider 失败：%s", exc)
            return None
        if provider is None:
            return None
        try:
            response = await provider.text_chat(
                prompt=prompt,
                system_prompt="",
                contexts=[],
                image_urls=[data_uri],
            )
        except Exception as exc:  # noqa: BLE001 - 纯文本 provider 会在这里 400
            self.audit.warn("视觉 provider 调用失败：%s", exc)
            return None
        return getattr(response, "completion_text", None)

    async def _describe_direct(
        self,
        data_uri: str,
        prompt: str,
        model: str,
        *,
        max_tokens: int = 800,
        disable_thinking: bool = True,
    ) -> str | None:
        if not self.base_url:
            return None
        url = chat_url(self.base_url)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, Any] = {
            "model": (model or self.model),
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
        }
        if disable_thinking:
            # DeepSeek 思考模型的推理会吃掉输出预算，打标时关掉最省
            payload["thinking"] = {"type": "disabled"}

        result = await self.http.post_json(url, payload, headers, timeout_s=30.0)
        if (
            not result.ok
            and disable_thinking
            and result.status in (400, 422)
        ):
            payload.pop("thinking", None)
            result = await self.http.post_json(url, payload, headers, timeout_s=30.0)
        if not result.ok:
            self.audit.warn(
                "视觉直连失败 status=%s error=%s", result.status, result.error
            )
            return None
        text = _chat_content(result.body)
        if not text and _finish_reason(result.body) == "length":
            payload["max_tokens"] = min(2000, max(1, max_tokens) * 2)
            result = await self.http.post_json(url, payload, headers, timeout_s=30.0)
            if result.ok:
                text = _chat_content(result.body)
        if not text:
            self.audit.warn("视觉直连返回空内容")
            return None
        return str(text)

    async def _via_direct(self, system: str, user: str, task: str = "") -> str | None:
        if not self.base_url:
            return None
        url = chat_url(self.base_url)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        mode, effort = thinking_policy(self.config, task)
        if mode == "enabled":
            payload["thinking"] = {"type": "enabled"}
            if effort:
                payload["reasoning_effort"] = effort
        elif mode == "disabled":
            payload["thinking"] = {"type": "disabled"}
        result = await self.http.post_json(url, payload, headers, timeout_s=60.0)
        if not result.ok and result.status in (400, 422) and (
            "thinking" in payload or "reasoning_effort" in payload
        ):
            payload.pop("thinking", None)
            payload.pop("reasoning_effort", None)
            result = await self.http.post_json(url, payload, headers, timeout_s=60.0)
        if not result.ok:
            self.stats["failures"] += 1
            self.audit.warn(
                "直连写作模型失败 status=%s error=%s", result.status, result.error
            )
            return None
        body = result.body
        content = ""
        if isinstance(body, dict):
            choices = body.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    content = str(message.get("content") or "")
        if not content:
            self.stats["failures"] += 1
            return None
        self.stats["direct"] += 1
        return content


def _chat_content(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0] if isinstance(choices[0], dict) else None
    if not first:
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    return str(content) if content else None


def _finish_reason(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "")
    return ""


def _clean_caption(text: str | None, max_chars: int) -> str | None:
    if not text:
        return None
    stripped = str(text).strip()
    if not stripped:
        return None
    body = stripped.splitlines()[0].strip()
    body = body.strip('"“”「」『』')
    if not body:
        return None
    if max_chars > 0:
        body = body[:max_chars]
    return body or None
