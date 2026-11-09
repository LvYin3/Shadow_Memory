"""发送：say / speak / send_image / silent 的执行与每轮发送登记。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..core.audit import Audit
from ..core.config import ConfigView

ConfigProvider = Callable[[str], ConfigView]
TTSResolver = Callable[[str, str], Awaitable[Any]]
SendTo = Callable[[str, Any], Awaitable[bool]]
RecordFactory = Callable[[str], Any]
ImageFactory = Callable[[bytes], Any]
TextFactory = Callable[[str], Any]

STATE_KEY = "shadow_memory_send_state"


@dataclass
class SendState:
    texts: list[str] = field(default_factory=list)
    image_texts: list[str] = field(default_factory=list)
    speaks: int = 0
    spoken_texts: list[str] = field(default_factory=list)
    images: int = 0
    silent: bool = False
    sent: bool = False
    errors: list[str] = field(default_factory=list)
    session: str = ""

    @property
    def segments(self) -> int:
        return len(self.texts) + self.speaks + self.images


def state_of(event: Any) -> SendState:
    existing = event.get_extra(STATE_KEY) if hasattr(event, "get_extra") else None
    if isinstance(existing, SendState):
        return existing
    state = SendState()
    if hasattr(event, "set_extra"):
        event.set_extra(STATE_KEY, state)
    return state


class Sender:
    def __init__(
        self,
        configs: ConfigProvider,
        audit: Audit,
        *,
        tts_resolver: TTSResolver | None = None,
        send_to: SendTo | None = None,
        record_factory: RecordFactory | None = None,
        image_factory: ImageFactory | None = None,
        text_factory: TextFactory | None = None,
    ) -> None:
        self.configs = configs
        self.audit = audit
        self._tts_resolver = tts_resolver
        self._send_to = send_to
        self._record_factory = record_factory
        self._image_factory = image_factory
        self._text_factory = text_factory

    async def say(
        self,
        event: Any,
        *,
        bot_id: str,
        text: str,
        target_session: str | None = None,
    ) -> str:
        cfg = self.configs(bot_id)
        state = state_of(event)
        body = (text or "").strip()
        if not body:
            return "（没说出口）"
        limit = cfg.int("interaction.max_segments", 3, lo=1, hi=10)
        if len(state.texts) >= limit:
            state.errors.append("segments_exceeded")
            return f"这一轮已经说了 {limit} 段，剩下的先咽回去。"
        try:
            chain = event.plain_result(body)
            if target_session and self._send_to is not None:
                ok = await self._send_to(target_session, chain)
                if not ok:
                    state.errors.append("send_failed")
                    return "这句没送出去。"
            else:
                await event.send(chain)
        except Exception as exc:  # noqa: BLE001 - 发送失败兜底
            state.errors.append(f"send_error:{type(exc).__name__}")
            self.audit.warn("say 发送失败：%s", exc)
            return "这句没送出去。"
        state.texts.append(body)
        state.sent = True
        return "已说"

    async def speak(
        self,
        event: Any,
        *,
        bot_id: str,
        umo: str,
        text: str,
        target_session: str | None = None,
    ) -> str:
        cfg = self.configs(bot_id)
        state = state_of(event)
        body = (text or "").strip()
        if not body:
            return "（没出声）"
        max_chars = cfg.int("interaction.speak_max_chars", 60, lo=1, hi=300)
        if len(body) > max_chars:
            state.errors.append("speak_too_long")
            return f"这句太长，说不出来（上限 {max_chars} 字），改用 say 吧。"
        if state.speaks >= 1:
            state.errors.append("speak_exceeded")
            return "这一轮已经用声音说过一句了。"
        provider = await self._resolve_tts(bot_id, umo)
        if provider is None or self._record_factory is None:
            self.audit.warn("没有可用的 TTS，说话降级为文字")
            return await self.say(event, bot_id=bot_id, text=body, target_session=target_session)
        try:
            path = await provider.get_audio(body)
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("TTS 合成失败，降级为文字：%s", exc)
            return await self.say(event, bot_id=bot_id, text=body, target_session=target_session)
        if not path:
            return await self.say(event, bot_id=bot_id, text=body, target_session=target_session)
        try:
            chain = event.chain_result([self._record_factory(path)])
            if target_session and self._send_to is not None:
                ok = await self._send_to(target_session, chain)
                if not ok:
                    raise RuntimeError("send_to returned False")
            else:
                await event.send(chain)
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("语音发送失败，降级为文字：%s", exc)
            return await self.say(event, bot_id=bot_id, text=body, target_session=target_session)
        state.speaks += 1
        state.spoken_texts.append(body)
        state.sent = True
        return "已说（语音）"

    async def send_image(
        self,
        event: Any,
        *,
        bot_id: str,
        data: bytes,
        caption: str = "",
        target_session: str | None = None,
    ) -> str:
        cfg = self.configs(bot_id)
        state = state_of(event)
        if not data:
            return "这张图没取到。"
        limit = cfg.int("interaction.send_image.max_per_turn", 2, lo=0, hi=10)
        if state.images >= limit:
            state.errors.append("images_exceeded")
            return f"这一轮已经发过 {limit} 张图了。"
        body = (caption or "").strip()
        max_caption = cfg.int("interaction.send_image.max_caption_chars", 200, lo=1, hi=2000)
        if len(body) > max_caption:
            state.errors.append("caption_too_long")
            return f"配文太长了（上限 {max_caption} 字）。"
        if self._image_factory is None:
            return "现在发不了图。"
        components: list[Any] = []
        if body and self._text_factory is not None:
            components.append(self._text_factory(body))
        components.append(self._image_factory(data))
        try:
            chain = event.chain_result(components)
            if target_session and self._send_to is not None:
                ok = await self._send_to(target_session, chain)
                if not ok:
                    raise RuntimeError("send_to returned False")
            else:
                await event.send(chain)
        except Exception as exc:  # noqa: BLE001
            state.errors.append(f"image_send_error:{type(exc).__name__}")
            self.audit.warn("发图失败：%s", exc)
            return "这张图没发出去。"
        state.images += 1
        state.sent = True
        if body:
            state.image_texts.append(body)
        return "发了。"

    def silent(self, event: Any) -> str:
        state = state_of(event)
        state.silent = True
        return "好"

    async def _resolve_tts(self, bot_id: str, umo: str) -> Any:
        if self._tts_resolver is None:
            return None
        try:
            return await self._tts_resolver(bot_id, umo)
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("解析 TTS 失败：%s", exc)
            return None
