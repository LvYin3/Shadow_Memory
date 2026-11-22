"""状态卡：她此刻在哪、在做什么、预计到何时、方不方便看手机。

两次唤醒之间，聊天侧只靠这张卡知道她的处境。卡会过期：过了预计结束时间，
“不方便看手机”立刻失效；再久一些，整张卡不再注入，免得陈旧状态一直挂着。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

MINUTE_MS = 60_000
HOUR_MS = 3_600_000

CHAT_BOUNDARY_LINE = (
    "- 聊到我自己的生活时，我只说现在的处境、已经发生过的事和我的打算；"
    "还没发生的事（尤其是别人的反应）不当成已经发生的来说。"
)

_BUSY_WORDS = ("不方便", "不能", "不行", "没法", "busy")


@dataclass(slots=True)
class StatusCard:
    where: str = ""
    doing: str = ""
    until: int = 0
    phone_ok: bool = True
    updated_at: int = 0

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "where": self.where,
            "doing": self.doing,
            "until": self.until,
            "phone_ok": self.phone_ok,
            "updated_at": self.updated_at,
        }


def load_card(raw: Any) -> StatusCard | None:
    """从库里存的 JSON 读回状态卡；读不出来就当没有。"""
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, dict):
        return None
    where = str(raw.get("where") or "").strip()
    doing = str(raw.get("doing") or "").strip()
    if not where and not doing:
        return None
    try:
        until = int(raw.get("until") or 0)
        updated_at = int(raw.get("updated_at") or 0)
    except (TypeError, ValueError):
        return None
    return StatusCard(
        where=where,
        doing=doing,
        until=until,
        phone_ok=bool(raw.get("phone_ok", True)),
        updated_at=updated_at,
    )


def build_card(raw: Any, now: int, *, max_minutes: int) -> StatusCard | None:
    """把背景模型收场时给的状态卡规整成内部结构；缺地点和活动就不成卡。"""
    if not isinstance(raw, dict):
        return None
    where = str(raw.get("where") or "").strip()[:40]
    doing = str(raw.get("doing") or "").strip()[:60]
    if not where and not doing:
        return None
    try:
        minutes = int(float(raw.get("minutes") or 0))
    except (TypeError, ValueError):
        minutes = 0
    minutes = min(max(minutes, 5), max(5, int(max_minutes)))
    phone_raw = raw.get("phone")
    if isinstance(phone_raw, bool):
        phone_ok = phone_raw
    else:
        phone = str(phone_raw or "").strip().lower()
        phone_ok = not any(word in phone for word in _BUSY_WORDS)
    return StatusCard(
        where=where,
        doing=doing,
        until=now + minutes * MINUTE_MS,
        phone_ok=phone_ok,
        updated_at=now,
    )


def format_clock(ts: int, now: int) -> str:
    """把时刻写成“16:30 / 明天 08:00 / 11月25日 08:00”。"""
    moment = datetime.fromtimestamp(ts / 1000.0)
    today = datetime.fromtimestamp(now / 1000.0).date()
    delta = (moment.date() - today).days
    clock = f"{moment.hour:02d}:{moment.minute:02d}"
    if delta == 0:
        return clock
    if delta == 1:
        return f"明天 {clock}"
    if delta == -1:
        return f"昨天 {clock}"
    return f"{moment.month}月{moment.day}日 {clock}"


def _situation(card: StatusCard) -> str:
    parts = []
    if card.where:
        parts.append(f"在{card.where}")
    if card.doing:
        parts.append(f"正在{card.doing}" if card.where else f"在{card.doing}")
    return "，".join(parts)


def render_for_chat(
    card: StatusCard | None, now: int, *, stale_hours: int = 6
) -> list[str]:
    """给聊天侧 ``# 我这会儿`` 用的第一人称行；卡太旧就什么也不给。"""
    if card is None:
        return []
    situation = _situation(card)
    if not situation:
        return []
    if not card.until or now <= card.until:
        line = f"- 我现在{situation}"
        if card.until:
            line += f"，大概到{format_clock(card.until, now)}"
        lines = [line + "。"]
        if not card.phone_ok:
            lines.append("- 这会儿不太方便看手机。")
        return lines
    if now - card.until <= max(0, int(stale_hours)) * HOUR_MS:
        return [
            f"- 我之前{situation}，按说{format_clock(card.until, now)}就结束了，"
            "这会儿应该已经忙完。"
        ]
    return []


def render_for_gm(card: StatusCard | None, now: int) -> str:
    """给背景模型看的客观描述。"""
    if card is None:
        return "（还没有状态卡）"
    parts = [f"地点：{card.where or '未写'}", f"在做：{card.doing or '未写'}"]
    if card.until:
        minutes = int(round((card.until - now) / MINUTE_MS))
        if minutes >= 0:
            parts.append(f"预计到 {format_clock(card.until, now)}（还有 {minutes} 分钟）")
        else:
            parts.append(
                f"原本预计到 {format_clock(card.until, now)}（已经过了 {-minutes} 分钟）"
            )
    parts.append(f"看手机：{'方便' if card.phone_ok else '不方便'}")
    return "；".join(parts)
