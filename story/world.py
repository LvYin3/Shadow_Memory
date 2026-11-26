"""世界种子（world_seed/v1）的校验，以及设定条目的检索与规整。

种子由用户在戏外写，运行时只读；演出中新确立的事实进设定集，只追加。
这里都是纯函数，读写库在 ``store.py``。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from ..memory.textutil import bigrams, overlap

SEED_VERSION = "world_seed/v1"

VISIBILITIES = ("known", "gm_only")
DEFAULT_VISIBILITY = "gm_only"
VISIBILITY_LABELS = {
    "known": "角色已知",
    "gm_only": "仅 GM",
}
_VISIBILITY_ALIASES = {
    "known": "known",
    "public": "known",
    "character": "known",
    "her": "known",
    "visible": "known",
    "角色已知": "known",
    "已知": "known",
    "公开": "known",
    "可见": "known",
    "gm_only": "gm_only",
    "gm": "gm_only",
    "secret": "gm_only",
    "hidden": "gm_only",
    "仅gm": "gm_only",
    "仅 gm": "gm_only",
    "秘密": "gm_only",
    "隐藏": "gm_only",
    "不可见": "gm_only",
    "角色不知情": "gm_only",
}

_NAME_SPLIT_RE = re.compile(r"[与和及、/／·（）()\[\]【】\s—\-:：]+")

CATEGORIES = ("person", "place", "org", "rule", "item", "other")
CATEGORY_LABELS = {
    "person": "人物",
    "place": "地点",
    "org": "组织",
    "rule": "规则",
    "item": "物品",
    "other": "其他",
}
_CATEGORY_ALIASES = {label: key for key, label in CATEGORY_LABELS.items()}
_CATEGORY_ALIASES.update(
    {
        "角色": "person",
        "人": "person",
        "场所": "place",
        "机构": "org",
        "团体": "org",
        "设定": "rule",
        "物件": "item",
        "东西": "item",
    }
)

OUTLINE_MIN = 50
OUTLINE_MAX = 3000
KNOWN_OUTLINE_MAX = 1000
TITLE_MAX = 40
NAME_MAX = 40
BODY_MAX = 500
MAX_ENTRIES = 200
MAX_HOOKS = 10
HOOK_MAX = 200

PLAY_BODY_MAX = 300
PLAY_MAX_PER_SCENE = 8
NOTE_TITLE_MAX = 40
NOTE_BODY_MAX = 600
NOTE_MAX_PER_SCENE = 6


class WorldSeedError(ValueError):
    """世界种子不合法。"""


def normalize_category(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in CATEGORIES:
        return text
    return _CATEGORY_ALIASES.get(text, "other")


def normalize_visibility(value: Any, *, default: str | None = None) -> str | None:
    """规整可见性标记；空值返回 default，无法识别时返回 default。"""
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return _VISIBILITY_ALIASES.get(text, default)


def validate_seed(data: Any) -> dict[str, Any]:
    """校验世界种子，返回可直接入库的结构；有问题时一次报全。"""
    if not isinstance(data, dict):
        raise WorldSeedError("世界种子不是 JSON 对象")
    errors: list[str] = []
    version = str(data.get("version") or "")
    if not version.startswith("world_seed"):
        errors.append(f"version 不认识：{version or '(空)'}（应为 {SEED_VERSION}）")

    title = str(data.get("title") or "").strip()
    if len(title) > TITLE_MAX:
        errors.append(f"title 超过 {TITLE_MAX} 字")

    outline = str(data.get("outline") or "").strip()
    if not (OUTLINE_MIN <= len(outline) <= OUTLINE_MAX):
        errors.append(
            f"outline 长度 {len(outline)} 不在 {OUTLINE_MIN}-{OUTLINE_MAX} 之间"
        )

    known_outline = str(data.get("known_outline") or "").strip()
    if len(known_outline) > KNOWN_OUTLINE_MAX:
        errors.append(f"known_outline 超过 {KNOWN_OUTLINE_MAX} 字")

    entries: list[dict[str, Any]] = []
    raw_entries = data.get("entries") or []
    if not isinstance(raw_entries, list):
        errors.append("entries 必须是数组")
        raw_entries = []
    if len(raw_entries) > MAX_ENTRIES:
        errors.append(f"entries 超过 {MAX_ENTRIES} 条")
    seen: set[str] = set()
    for index, item in enumerate(raw_entries[:MAX_ENTRIES]):
        if not isinstance(item, dict):
            errors.append(f"entries[{index}] 不是对象")
            continue
        name = str(item.get("name") or "").strip()
        body = str(item.get("body") or "").strip()
        if not name or len(name) > NAME_MAX:
            errors.append(f"entries[{index}] name 为空或超过 {NAME_MAX} 字")
            continue
        if not body or len(body) > BODY_MAX:
            errors.append(f"entries[{index}]（{name}）body 为空或超过 {BODY_MAX} 字")
            continue
        if name in seen:
            errors.append(f"entries[{index}] name 重复：{name}")
            continue
        raw_vis = item.get("visibility")
        visibility: str | None = None
        if raw_vis is not None and str(raw_vis).strip():
            visibility = normalize_visibility(raw_vis, default=None)
            if visibility is None:
                errors.append(
                    f"entries[{index}]（{name}）visibility 不认识：{raw_vis}（应为 known 或 gm_only）"
                )
                continue
        seen.add(name)
        entries.append(
            {
                "category": normalize_category(item.get("category")),
                "name": name,
                "body": body,
                "visibility": visibility,
            }
        )

    hooks: list[str] = []
    raw_hooks = data.get("hooks") or []
    if not isinstance(raw_hooks, list):
        errors.append("hooks 必须是数组")
        raw_hooks = []
    if len(raw_hooks) > MAX_HOOKS:
        errors.append(f"hooks 超过 {MAX_HOOKS} 条")
    for index, item in enumerate(raw_hooks[:MAX_HOOKS]):
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > HOOK_MAX:
            errors.append(f"hooks[{index}] 超过 {HOOK_MAX} 字")
            continue
        hooks.append(text)

    opening: dict[str, str] = {}
    raw_opening = data.get("opening")
    if raw_opening is not None:
        if not isinstance(raw_opening, dict):
            errors.append("opening 必须是对象")
        else:
            opening = {
                "where": str(raw_opening.get("where") or "").strip()[:40],
                "doing": str(raw_opening.get("doing") or "").strip()[:60],
            }
            if not opening["where"] and not opening["doing"]:
                opening = {}

    if errors:
        raise WorldSeedError("；".join(errors[:12]))
    return {
        "version": SEED_VERSION,
        "title": title,
        "outline": outline,
        "known_outline": known_outline,
        "entries": entries,
        "hooks": hooks,
        "opening": opening,
    }


def split_name_terms(name: str) -> list[str]:
    """拆解复合条目标题中的关键子词（如「流光异术与虚刃」→「流光异术」「虚刃」）。"""
    clean = str(name or "").strip()
    if not clean:
        return []
    terms: list[str] = [clean]
    for part in _NAME_SPLIT_RE.split(clean):
        part = part.strip()
        if len(part) >= 2 and part not in terms:
            terms.append(part)
    return terms


def score_entry(entry: dict[str, Any], query: str, tokens: set[str] | None = None) -> float:
    """对单条设定做复合词 + bigram 相关性打分。"""
    text = str(query or "").strip()
    if not text:
        return 0.0
    lower_q = text.lower()
    q_tokens = tokens if tokens is not None else bigrams(text)
    name = str(entry.get("name") or "").strip()
    body = str(entry.get("body") or "").strip()
    score = 0.0
    if name and name.lower() in lower_q:
        score += 2.0
    else:
        sub_hits = 0
        for term in split_name_terms(name)[1:]:
            if term.lower() in lower_q:
                score += 1.5 if sub_hits == 0 else 0.5
                sub_hits += 1
    if q_tokens:
        if name:
            score += 1.2 * overlap(q_tokens, name)
        if body:
            score += 0.5 * overlap(q_tokens, body)
    return score


def search_scored(
    entries: Iterable[dict[str, Any]],
    query: str,
    limit: int,
    *,
    min_score: float = 0.0,
) -> list[tuple[float, dict[str, Any]]]:
    """按复合标题子词与二元组重合度打分，返回 (score, entry) 列表。"""
    if limit <= 0:
        return []
    text = str(query or "").strip()
    if not text:
        return []
    tokens = bigrams(text)
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for index, entry in enumerate(entries):
        score = score_entry(entry, text, tokens)
        if score <= min_score:
            continue
        scored.append((score, index, entry))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [(score, entry) for score, _, entry in scored[:limit]]


def search(
    entries: Iterable[dict[str, Any]], query: str, limit: int
) -> list[dict[str, Any]]:
    """按名字命中、复合词拆解和二元组重合给设定条目打分，取最相关的几条。"""
    return [entry for _, entry in search_scored(entries, query, limit, min_score=0.0)]


def find_by_names(
    entries: Iterable[dict[str, Any]], names: Iterable[Any], limit: int = 6
) -> list[dict[str, Any]]:
    """背景模型点名要查的设定：先精确匹配名字，再做包含匹配。"""
    pool = list(entries)
    wanted = [str(name or "").strip() for name in names if str(name or "").strip()]
    found: list[dict[str, Any]] = []
    seen: set[int] = set()
    for name in wanted:
        exact = [entry for entry in pool if str(entry.get("name") or "") == name]
        loose = [
            entry
            for entry in pool
            if name in str(entry.get("name") or "") or str(entry.get("name") or "") in name
        ]
        for entry in exact or loose:
            key = id(entry)
            if key in seen:
                continue
            seen.add(key)
            found.append(entry)
            if len(found) >= limit:
                return found
    return found


def clean_canon(raw: Any) -> list[dict[str, str]]:
    """规整背景模型收场时登记的新设定；未显式标注可见性时一律默认 gm_only。"""
    if not isinstance(raw, list):
        return []
    items: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:NAME_MAX]
        body = str(item.get("body") or "").strip()[:PLAY_BODY_MAX]
        if not name or not body or (name, body) in seen:
            continue
        seen.add((name, body))
        visibility = normalize_visibility(item.get("visibility"), default=DEFAULT_VISIBILITY) or DEFAULT_VISIBILITY
        items.append(
            {
                "category": normalize_category(item.get("category")),
                "name": name,
                "body": body,
                "visibility": visibility,
            }
        )
        if len(items) >= PLAY_MAX_PER_SCENE:
            break
    return items


def clean_notes(raw: Any) -> list[dict[str, str]]:
    """规整背景模型要新增、改写或删除（body 为空）的笔记。"""
    if not isinstance(raw, list):
        return []
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:NOTE_TITLE_MAX]
        if not title or title in seen:
            continue
        seen.add(title)
        items.append({"title": title, "body": str(item.get("body") or "").strip()[:NOTE_BODY_MAX]})
        if len(items) >= NOTE_MAX_PER_SCENE:
            break
    return items


def render_entry(entry: dict[str, Any]) -> str:
    label = CATEGORY_LABELS.get(str(entry.get("category") or "other"), "其他")
    return f"- [{label}] {entry.get('name')}：{entry.get('body')}"
