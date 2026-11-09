from __future__ import annotations

import logging

from astrbot_plugin_shadow_memory.core.audit import Audit


def _logger(name: str) -> logging.Logger:
    return logging.getLogger(f"test.shadow_memory.audit.{name}")


def test_debug_is_off_by_default(caplog):
    caplog.set_level(logging.DEBUG)
    audit = Audit(_logger("off"))
    audit.debug("persona_inject_start", bot_id="b1")
    assert not [r for r in caplog.records if r.name == "test.shadow_memory.audit.off"]


def test_debug_outputs_only_after_switch_on(caplog):
    caplog.set_level(logging.DEBUG)
    audit = Audit(_logger("on"))
    audit.set_debug(True)
    audit.debug("persona_inject_start", bot_id="b1")
    records = [r for r in caplog.records if r.name == "test.shadow_memory.audit.on"]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert "persona_inject_start" in records[0].getMessage()


def test_normal_events_stay_info_even_with_debug_on(caplog):
    caplog.set_level(logging.INFO)
    audit = Audit(_logger("event"), debug=True)
    audit.event("environment_injected", bot_id="b1")
    records = [r for r in caplog.records if r.name == "test.shadow_memory.audit.event"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
