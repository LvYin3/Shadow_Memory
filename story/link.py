"""聊天与故事之间的纯规则：远方动作、主动消息的种类、安静时段、唤醒时间。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

REMOTE_KINDS = ("parcel", "food", "money")
REMOTE_LABELS = {"parcel": "寄东西", "food": "点外卖", "money": "转账"}
REMOTE_QUESTIONS = {
    "parcel": (
        "这条消息是不是发消息的人在说，自己已经给对方寄出或送出一件实物？"
        "概率越高越像。点外卖和转账不算。"
    ),
    "food": "这条消息是不是发消息的人在说，自己已经给对方点了外卖或外送食物？概率越高越像。",
    "money": "这条消息是不是发消息的人在说，自己已经给对方转了一笔钱？概率越高越像。",
}
OUTCOMES = {
    "到了": "arrived",
    "arrived": "arrived",
    "没到": "missed",
    "missed": "missed",
    "她没管": "ignored",
    "ignored": "ignored",
}
_QUIET = re.compile(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})")


def remote_questions() -> dict[str, dict[str, str]]:
    return {kind: {"type": "noul", "instructions": text} for kind, text in REMOTE_QUESTIONS.items()}


def pick_remote(scores: dict[str, Any], threshold: float) -> str:
    """过阈值的最高一项；打平或没有合格项就不记账。"""
    best = ""
    best_score = -1.0
    tie = False
    for kind in REMOTE_KINDS:
        raw = scores.get(kind)
        if isinstance(raw, dict):
            raw = raw.get("noul")
        try:
            score = float(raw)
        except (TypeError, ValueError):
            continue
        if score > best_score:
            best, best_score, tie = kind, score, False
        elif score == best_score and best:
            tie = True
    if tie or best_score < float(threshold):
        return ""
    return best


def quiet_active(spec: str, now_ms: int) -> bool:
    """安静时段。留空或写不出来就不启用。跨过午夜的时段（23:00-08:00）也认。"""
    match = _QUIET.fullmatch(str(spec or "").strip())
    if match is None:
        return False
    start_h, start_m, end_h, end_m = (int(part) for part in match.groups())
    if not (0 <= start_h <= 23 and 0 <= end_h <= 23 and start_m < 60 and end_m < 60):
        return False
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start == end:
        return False
    moment = datetime.fromtimestamp(now_ms / 1000.0)
    now_min = moment.hour * 60 + moment.minute
    if start < end:
        return start <= now_min < end
    return now_min >= start or now_min < end


def group_names(spec: str) -> set[str]:
    return {part.strip() for part in str(spec or "").split(",") if part.strip()}


def classify_outbound(umo: str, *, settled: set[str], unread: set[str]) -> str:
    """回告优先于补回；都不是就是自发分享。"""
    if umo in settled:
        return "report"
    if umo in unread:
        return "catchup"
    return "share"


def parse_messages(raw: Any) -> list[tuple[str, str]]:
    if not isinstance(raw, list):
        return []
    found: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        target = str(item.get("to") or "").strip()[:80]
        text = str(item.get("text") or "").strip()[:300]
        if target and text:
            found.append((target, text))
    return found


def wake_request(raw: Any) -> tuple[str, int]:
    """收场给的下次时间。

    返回 ``("at", 分钟)`` 或 ``("ask", 0)``。0 是马上，不是没写。
    """
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return "ask", 0
    try:
        minutes = int(float(raw))
    except (TypeError, ValueError):
        return "ask", 0
    if minutes < 0:
        return "ask", 0
    return "at", minutes


def clamp_wake(minutes: int, *, min_wake: int, max_wake: int) -> int:
    value = max(0, int(minutes))
    if min_wake > 0:
        value = max(value, min_wake)
    if max_wake > 0:
        value = min(value, max_wake)
    return value


def wake_span(min_wake: int, max_wake: int) -> str:
    if min_wake <= 0 and max_wake <= 0:
        return (
            "不限制。写 0 表示这场一结束就可以继续；"
            "这个字段不写，会再问一次，还是不写就不再自动醒"
        )
    if min_wake <= 0:
        return f"0–{max_wake}。写 0 表示这场一结束就可以继续"
    if max_wake <= 0:
        return f"至少 {min_wake}，没有上限"
    return f"{min_wake}–{max_wake}"


def quoted_by(name: str, text: str) -> bool:
    """这个名字后面紧跟着引号里的话，就算替他说了。"""
    if not name:
        return False
    pattern = re.compile(
        re.escape(name) + r"(?:说|问|答|喊|道|回|发来|传来)?[：:，,\s]*[“「\"]"
    )
    return pattern.search(text) is not None


def render_brief(
    intents: list[dict[str, Any]],
    unreads: list[dict[str, Any]],
    remotes: list[dict[str, Any]],
) -> str:
    blocks: list[str] = []
    if intents:
        lines = [
            f"- #{row['id']} 对 {row.get('display_name') or '对方'}：{row.get('body')}"
            for row in intents
        ]
        blocks.append("# 还没裁定的意图（她答应要做的）\n" + "\n".join(lines))
    if unreads:
        lines = [
            f"- 对 {row.get('display_name') or '对方'}：{row.get('text')}" for row in unreads
        ]
        blocks.append("# 没回的私聊（只留最新的）\n" + "\n".join(lines))
    if remotes:
        lines = [
            f"- #{row['id']} {row.get('display_name')} 说了"
            f"（{REMOTE_LABELS.get(str(row.get('kind')), '一件事')}）：{row.get('claim')}"
            for row in remotes
        ]
        blocks.append(
            "# 远方动作（只是他说了，还不一定到；到不到由你和骰子定）\n" + "\n".join(lines)
        )
    return "\n\n".join(blocks)
