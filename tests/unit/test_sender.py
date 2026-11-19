from __future__ import annotations

import os
import tempfile
import wave
from pathlib import Path

import pytest

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.interaction.result import should_drop_final_text
from astrbot_plugin_shadow_memory.interaction.sender import Sender, state_of

from ..fakes import FakeChain, FakeEvent, FakeTTS


@pytest.fixture
def sender(configs):
    tts = {"provider": FakeTTS()}
    recorded: list[str] = []
    sent_to: list[tuple[str, object]] = []

    async def resolver(bot_id, umo):
        return tts["provider"]

    async def send_to(umo, chain):
        sent_to.append((umo, chain))
        return True

    instance = Sender(
        configs,
        Audit(),
        tts_resolver=resolver,
        send_to=send_to,
        record_factory=lambda path: FakeChain(str(path), "record"),
    )
    instance.recorded = recorded  # type: ignore[attr-defined]
    instance.sent_to = sent_to  # type: ignore[attr-defined]
    instance.tts = tts  # type: ignore[attr-defined]
    return instance


async def test_say_three_segments_then_reject(sender):
    event = FakeEvent()
    for index in range(3):
        result = await sender.say(event, bot_id="bot1", text=f"第{index}句")
        assert result == "已说"
    rejected = await sender.say(event, bot_id="bot1", text="第四句")
    assert "3 段" in rejected
    state = state_of(event)
    assert len(state.texts) == 3
    assert len(event.sent) == 3


async def test_say_rejects_empty(sender):
    event = FakeEvent()
    assert "没说出口" in await sender.say(event, bot_id="bot1", text="   ")
    assert event.sent == []


async def test_say_cleans_trailing_text_tag(sender):
    event = FakeEvent()
    result = await sender.say(event, bot_id="bot1", text="你好</text>")
    assert result == "已说"
    assert event.sent[0].chain[0].text == "你好"


async def test_say_after_cleaning_empty_does_not_send(sender):
    event = FakeEvent()
    result = await sender.say(event, bot_id="bot1", text="</text>")
    assert "没说出口" in result
    assert event.sent == []


async def test_speak_cleans_trailing_text_tag(sender):
    event = FakeEvent()
    result = await sender.speak(event, bot_id="bot1", umo="umo1", text="你好</text>")
    assert result == "已说（语音）"
    state = state_of(event)
    assert state.spoken_texts == ["你好"]


async def test_speak_length_limit(sender):
    event = FakeEvent()
    result = await sender.speak(event, bot_id="bot1", umo="umo1", text="字" * 61)
    assert "太长" in result
    assert event.sent == []


async def test_speak_only_once_per_turn(sender):
    event = FakeEvent()
    assert await sender.speak(event, bot_id="bot1", umo="umo1", text="你好呀") == "已说（语音）"
    assert "已经用声音" in await sender.speak(event, bot_id="bot1", umo="umo1", text="再说一句")


async def _voice_wav() -> Path:
    fd, name = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    path = Path(name)
    samples = [1000, -1000] * 3200
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(32000)
        wav.writeframes(bytes().join(
            int(sample).to_bytes(2, "little", signed=True) for sample in samples
        ))
    return path


async def test_speak_official_normalizes_before_send(sender):
    source = await _voice_wav()

    async def precheck(path):
        assert path.exists()
        return True, ""

    sender._silk_precheck = precheck
    sender.tts["provider"] = FakeTTS(path=str(source))
    event = FakeEvent(platform_name="qq_official", message_type="FriendMessage", group=False)
    try:
        result = await sender.speak(event, bot_id="bot1", umo="umo1", text="你好呀")
        assert result == "已说（语音）"
        assert state_of(event).sent is True
        assert len(event.sent) == 1
        sent_path = event.sent[0].chain[0].text
        assert sent_path != str(source)
        assert not Path(sent_path).exists()
    finally:
        source.unlink(missing_ok=True)


async def test_speak_official_precheck_failure_does_not_fallback(sender):
    source = await _voice_wav()

    async def precheck(path):
        return False, "silk failed"

    sender._silk_precheck = precheck
    sender.tts["provider"] = FakeTTS(path=str(source))
    event = FakeEvent(platform_name="qq_official", message_type="FriendMessage", group=False)
    try:
        result = await sender.speak(event, bot_id="bot1", umo="umo1", text="你好呀")
        assert "转换失败" in result
        assert state_of(event).sent is False
        assert state_of(event).texts == []
        assert event.sent == []
    finally:
        source.unlink(missing_ok=True)


async def test_speak_falls_back_to_text_without_tts(sender):
    sender.tts["provider"] = None
    event = FakeEvent()
    result = await sender.speak(event, bot_id="bot1", umo="umo1", text="你好呀")
    assert result == "已说"
    state = state_of(event)
    assert state.speaks == 0
    assert state.texts == ["你好呀"]


async def test_speak_falls_back_when_tts_raises(sender):
    sender.tts["provider"] = FakeTTS(fail=True)
    event = FakeEvent()
    result = await sender.speak(event, bot_id="bot1", umo="umo1", text="你好呀")
    assert result == "已说"
    assert state_of(event).texts == ["你好呀"]


async def test_silent_and_say_are_not_exclusive(sender):
    event = FakeEvent()
    sender.silent(event)
    await sender.say(event, bot_id="bot1", text="还是说一句")
    state = state_of(event)
    assert state.silent is True and state.sent is True


async def test_say_to_another_session_uses_send_to(sender):
    event = FakeEvent()
    result = await sender.say(
        event, bot_id="bot1", text="给别的群说一句", target_session="aiocqhttp:GroupMessage:g2"
    )
    assert result == "已说"
    assert sender.sent_to and sender.sent_to[0][0] == "aiocqhttp:GroupMessage:g2"
    assert event.sent == []


async def test_send_failure_returns_error(sender):
    async def failing_send_to(umo, chain):
        return False

    sender._send_to = failing_send_to
    event = FakeEvent()
    result = await sender.say(
        event, bot_id="bot1", text="发不出去", target_session="aiocqhttp:GroupMessage:g2"
    )
    assert "没送出去" in result


def test_result_rule():
    assert should_drop_final_text(sent=True, silent=False, fallback=False) is True
    assert should_drop_final_text(sent=False, silent=True, fallback=True) is True
    assert should_drop_final_text(sent=False, silent=False, fallback=False) is True
    assert should_drop_final_text(sent=False, silent=False, fallback=True) is False
