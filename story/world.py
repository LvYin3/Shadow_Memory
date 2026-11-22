"""世界种子（world_seed/v1）的校验，以及设定条目的检索与规整。

种子由用户在戏外写，运行时只读；演出中新确立的事实进设定集，只追加。
这里都是纯函数，读写库在 ``store.py``。
"""

from __future__ import annotations

from typing import Any, Iterable

from ..memory.textutil import bigrams, overlap

SEED_VERSION = "world_seed/v1"

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

    entries: list[dict[str, str]] = []
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
        seen.add(name)
        entries.append(
            {"category": normalize_category(item.get("category")), "name": name, "body": body}
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
        "entries": entries,
        "hooks": hooks,
        "opening": opening,
    }


def search(
    entries: Iterable[dict[str, Any]], query: str, limit: int
) -> list[dict[str, Any]]:
    """按名字命中和二元组重合给设定条目打分，取最相关的几条。"""
    if limit <= 0:
        return []
    text = query or ""
    tokens = bigrams(text)
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for index, entry in enumerate(entries):
        name = str(entry.get("name") or "")
        body = str(entry.get("body") or "")
        score = overlap(tokens, f"{name} {body}")
        if name and name in text:
            score += 1.0
        if score <= 0:
            continue
        scored.append((score, index, entry))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [entry for _, _, entry in scored[:limit]]


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
    """规整背景模型收场时登记的新设定。"""
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
        items.append(
            {"category": normalize_category(item.get("category")), "name": name, "body": body}
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
