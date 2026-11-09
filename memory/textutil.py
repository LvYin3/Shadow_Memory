"""文本小工具：bigram 相似度、注入截断、问句判断。"""

from __future__ import annotations


def bigrams(text: str) -> set[str]:
    body = "".join(char.lower() for char in (text or "") if char.isalnum())
    if len(body) < 2:
        return {body} if body else set()
    return {body[index : index + 2] for index in range(len(body) - 1)}


def overlap(tokens: set[str], text: str) -> float:
    if not tokens:
        return 0.0
    body = (text or "").lower()
    score = 0.0
    multi = {token for token in tokens if len(token) > 1}
    if multi:
        other = bigrams(text)
        if other:
            score += len(multi & other) / (len(multi) ** 0.5 * len(other) ** 0.5 + 1e-6)
    singles = {token for token in tokens if len(token) == 1}
    if singles and any(token in body for token in singles):
        score += 0.5
    return score


def trim_at_boundary(text: str, limit: int) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    head = body[:limit]
    for mark in ("。", "！", "？", "…", ".", "!", "?", "；", ";"):
        index = head.rfind(mark)
        if index >= max(4, limit // 2):
            return head[: index + 1]
    return head


def ends_with_question(text: str | None) -> bool:
    body = (text or "").strip()
    if not body:
        return False
    return body.rstrip("\"'）)】」』”’").endswith(("?", "？"))
