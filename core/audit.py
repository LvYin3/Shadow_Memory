"""审计与脱敏日志。"""

from __future__ import annotations

import json
import logging
from typing import Any


def redact(text: str | None, head: int = 40) -> str:
    """日志里只保留长度与前 N 个字，避免把聊天内容整段落盘。"""
    if not text:
        return ""
    body = str(text).replace("\n", " ")
    return f"{body[:head]}…(len={len(body)})"


class Audit:
    def __init__(self, logger: logging.Logger | None = None, debug: bool = False) -> None:
        self._logger = logger or logging.getLogger("astrbot.plugin.shadow_memory")
        self._debug = debug

    def set_debug(self, enabled: bool) -> None:
        """运行时切换 DEBUG 诊断日志；普通 event 仍按 INFO 输出。"""
        self._debug = bool(enabled)

    def event(self, kind: str, **fields: Any) -> None:
        payload = {key: value for key, value in fields.items() if value is not None}
        try:
            line = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            line = str(payload)
        self._logger.info("[mem] %s %s", kind, line)

    def debug(self, kind: str, **fields: Any) -> None:
        """只有配置开关打开时才输出 DEBUG 诊断。"""
        if not self._debug:
            return
        payload = {key: value for key, value in fields.items() if value is not None}
        try:
            line = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            line = str(payload)
        self._logger.debug("[mem] %s %s", kind, line)

    def warn(self, message: str, *args: Any) -> None:
        self._logger.warning("[mem] " + message, *args)

    def error(self, message: str, *args: Any) -> None:
        self._logger.error("[mem] " + message, *args)
