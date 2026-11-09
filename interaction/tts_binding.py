"""TTS 绑定解析：DB 显式优先，空字符串表示静音，配置兜底。"""

from __future__ import annotations

from typing import Any, Mapping


def resolve_tts_provider_id(
    kv_value: Any, mapping: Mapping[str, Any] | None, bot_id: str
) -> str:
    if kv_value is not None:
        return str(kv_value).strip()
    return str((mapping or {}).get(bot_id) or "").strip()
