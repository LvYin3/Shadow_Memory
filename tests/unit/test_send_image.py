from __future__ import annotations

import pytest

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.interaction.result import should_drop_final_text
from astrbot_plugin_shadow_memory.interaction.sender import Sender, state_of

from ..fakes import FakeEvent

PNG = b"\x89PNG\r\n\x1a\n" + b"png-body" * 8


class FakeImageComp:
    type = "image"

    def __init__(self, data: bytes) -> None:
        self.data = data


def make_sender(make_configs, overrides=None):
    configs = make_configs(overrides)
    sent_to: list = []

    async def send_to(umo, chain):
        sent_to.append((umo, chain))
        return True

    sender = Sender(
        configs,
        Audit(),
        send_to=send_to,
        record_factory=lambda path: ("record", path),
        image_factory=lambda data: FakeImageComp(data),
        text_factory=lambda text: ("plain", text),
    )
    sender.sent_to = sent_to  # type: ignore[attr-defined]
    return sender


async def test_send_image_builds_one_message_with_caption(make_configs):
    sender = make_sender(make_configs)
    event = FakeEvent()
    result = await sender.send_image(event, bot_id="bot1", data=PNG, caption="你看")
    assert result == "发了。"
    state = state_of(event)
    assert state.images == 1
    assert state.sent is True
    assert state.image_texts == ["你看"]
    chain = event.sent[0]
    assert chain.chain[0] == ("plain", "你看")
    assert isinstance(chain.chain[1], FakeImageComp)
    assert chain.chain[1].data == PNG


async def test_send_image_without_caption(make_configs):
    sender = make_sender(make_configs)
    event = FakeEvent()
    assert await sender.send_image(event, bot_id="bot1", data=PNG) == "发了。"
    chain = event.sent[0]
    assert len(chain.chain) == 1
    assert state_of(event).image_texts == []


async def test_send_image_per_turn_limit(make_configs):
    sender = make_sender(make_configs, {"interaction": {"send_image": {"max_per_turn": 1}}})
    event = FakeEvent()
    assert await sender.send_image(event, bot_id="bot1", data=PNG) == "发了。"
    second = await sender.send_image(event, bot_id="bot1", data=PNG)
    assert "1 张图" in second
    assert state_of(event).images == 1


async def test_send_image_caption_too_long(make_configs):
    sender = make_sender(
        make_configs, {"interaction": {"send_image": {"max_caption_chars": 4}}}
    )
    event = FakeEvent()
    result = await sender.send_image(event, bot_id="bot1", data=PNG, caption="太长的配文")
    assert "配文太长" in result
    assert event.sent == []


async def test_send_image_to_another_session(make_configs):
    sender = make_sender(make_configs)
    event = FakeEvent()
    result = await sender.send_image(
        event,
        bot_id="bot1",
        data=PNG,
        caption="给你",
        target_session="aiocqhttp:GroupMessage:g2",
    )
    assert result == "发了。"
    assert sender.sent_to and sender.sent_to[0][0] == "aiocqhttp:GroupMessage:g2"
    assert event.sent == []


async def test_send_image_failure_leaves_state_untouched(make_configs):
    sender = make_sender(make_configs)
    event = FakeEvent()

    async def boom(chain):
        raise RuntimeError("network down")

    event.send = boom  # type: ignore[assignment]
    result = await sender.send_image(event, bot_id="bot1", data=PNG)
    assert "没发出去" in result
    state = state_of(event)
    assert state.images == 0
    assert state.sent is False


async def test_send_image_without_factory(make_configs):
    sender = Sender(make_configs(), Audit())
    event = FakeEvent()
    assert await sender.send_image(event, bot_id="bot1", data=PNG) == "现在发不了图。"


async def test_send_image_triggers_final_text_drop(make_configs):
    sender = make_sender(make_configs)
    event = FakeEvent()
    await sender.send_image(event, bot_id="bot1", data=PNG, caption="看图")
    state = state_of(event)
    assert should_drop_final_text(sent=state.sent, silent=state.silent, fallback=True) is True


async def test_image_segments_counted(make_configs):
    sender = make_sender(make_configs)
    event = FakeEvent()
    await sender.say(event, bot_id="bot1", text="先说一句")
    await sender.send_image(event, bot_id="bot1", data=PNG)
    assert state_of(event).segments == 2
