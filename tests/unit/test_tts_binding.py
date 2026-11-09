from __future__ import annotations

from astrbot_plugin_shadow_memory.interaction.tts_binding import resolve_tts_provider_id


def test_db_explicit_wins():
    assert resolve_tts_provider_id("tts-a", {"bot1": "tts-b"}, "bot1") == "tts-a"


def test_empty_db_means_mute():
    assert resolve_tts_provider_id("", {"bot1": "tts-b"}, "bot1") == ""


def test_missing_db_falls_back_to_config():
    assert resolve_tts_provider_id(None, {"bot1": "tts-b"}, "bot1") == "tts-b"
    assert resolve_tts_provider_id(None, None, "bot1") == ""
    assert resolve_tts_provider_id(None, {}, "bot1") == ""
