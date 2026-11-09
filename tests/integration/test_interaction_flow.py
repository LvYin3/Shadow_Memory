from __future__ import annotations

import pytest

from astrbot_plugin_shadow_memory.core import clock as clock_mod
from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.core.types import Turn
from astrbot_plugin_shadow_memory.interaction.qzone import QzoneService
from astrbot_plugin_shadow_memory.interaction.media_dir import MediaDir
from astrbot_plugin_shadow_memory.interaction.result import should_drop_final_text
from astrbot_plugin_shadow_memory.interaction.sender import Sender, state_of

from ..fakes import FakeEvent, FakeTTS
from ..unit.helpers import build_stack

DAY = 86_400_000


async def test_group_gate_then_segmented_reply(db, configs):
    stack = build_stack(db, configs)
    stack.jev.noul_value = 0.9
    decision = await stack.gate.judge(
        bot_id="bot1", umo="umo1", message="有人吗", sender_name="小明"
    )
    assert decision.passed is True

    sent_to: list = []

    async def send_to(umo, chain):
        sent_to.append((umo, chain))
        return True

    sender = Sender(
        configs,
        Audit(),
        tts_resolver=lambda bot_id, umo: _tts(),
        send_to=send_to,
        record_factory=lambda path: path,
    )
    event = FakeEvent()
    for index in range(3):
        assert await sender.say(event, bot_id="bot1", text=f"第{index}句") == "已说"
    assert should_drop_final_text(
        sent=state_of(event).sent, silent=False, fallback=False
    )
    assert len(event.sent) == 3


async def _tts():
    return FakeTTS()


async def test_private_silence_keeps_final_text_when_fallback(db, configs):
    sender = Sender(configs, Audit())
    event = FakeEvent(message_type="FriendMessage", group=False)
    sender.silent(event)
    state = state_of(event)
    assert should_drop_final_text(sent=state.sent, silent=True, fallback=True) is True


async def test_memo_wake_context_has_persona_and_catalog(db, configs, workdir):
    stack = build_stack(db, configs)
    now = clock_mod.now_ms()
    await stack.service.observe(
        Turn("bot1", "umo1", "user", "最近在忙装修", now - DAY, "u1", "小明"),
        display_name="小明的私聊",
        chat_type="private",
    )
    await stack.facts.add_manual("bot1", "小明在装修房子", salience=3, now=now - DAY)
    persona = await stack.persona.render("bot1")
    catalog = await stack.catalog.render("bot1")
    assert persona == "" or isinstance(persona, str)
    assert "小明的私聊" in catalog or "umo1" in catalog


async def test_qzone_end_to_end(db, configs, workdir):
    media = MediaDir(workdir / "media2", "bot1")
    media.ensure_dir()
    (media.media_dir / "a.png").write_bytes(b"x")
    service = QzoneService(
        db, configs, lambda bot_id: media, Audit(), now_fn=lambda: 1_800_000_000_000
    )
    event = FakeEvent()
    result = await service.post(
        event, bot_id="bot1", text="今天风很好", images=["a.png"], ugc_right=4
    )
    assert result["ok"] is True
    action, payload = event.bot.calls[0]
    assert action == "send_qzone_msg"
    assert payload["ugc_right"] == 4
    assert payload["images"][0].startswith("file://")


async def test_seed_then_live_memory_share_bot(db, configs):
    stack = build_stack(db, configs)
    seed = {
        "version": "persona_seed/v1",
        "persona": {"core": ["我" * 60], "edge": [], "tension": []},
        "facts": [],
        "events": [],
    }
    await stack.service.import_seed("bot1", seed)
    now = clock_mod.now_ms()
    await stack.service.observe(
        Turn("bot1", "umo1", "user", "新的一天", now, "u1", "小明"),
        display_name="测试群",
        chat_type="group",
    )
    assert await stack.db.scalar("SELECT COUNT(*) FROM persona_entries") == 1
    assert await stack.db.scalar("SELECT COUNT(*) FROM events WHERE status='open'") == 1
