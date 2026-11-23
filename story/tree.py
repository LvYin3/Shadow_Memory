"""故事树：未了结的线，以及没挂上线的日常场。

主持人只提交线。日常节点由程序按场景补上。校验不过就留下上一棵树。
"""

from __future__ import annotations

import json
from typing import Any

TITLE_MAX = 40
THREAD_MAX = 40
NAME_MAX = 40


def empty() -> dict[str, Any]:
    return {"threads": [], "daily": {"recent": [], "older": []}}


def loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return empty()
    if not isinstance(raw, dict):
        return empty()
    threads = raw.get("threads") if isinstance(raw.get("threads"), list) else []
    daily = raw.get("daily") if isinstance(raw.get("daily"), dict) else {}
    recent = daily.get("recent") if isinstance(daily.get("recent"), list) else []
    older = daily.get("older") if isinstance(daily.get("older"), list) else []
    return {"threads": threads, "daily": {"recent": recent, "older": older}}


def clean_threads(
    raw: Any,
    *,
    people: set[str],
    canon: set[str],
    seed: set[str],
    scene_ids: set[int],
    current_scene_id: int | None = None,
) -> dict[str, Any] | None:
    """规整主持人交来的线。结构不像一棵树就返回 None，调用方保留旧树。"""
    if not isinstance(raw, dict):
        return None
    rows = raw.get("threads")
    if not isinstance(rows, list):
        return None
    threads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())[:TITLE_MAX]
        if not title or title in seen:
            continue
        seen.add(title)
        threads.append(
            {
                "title": title,
                "people": _names(item.get("people"), people),
                "canon": _names(item.get("canon"), canon),
                "seed": _names(item.get("seed"), seed),
                "scenes": _scenes(item.get("scenes"), scene_ids, current_scene_id),
            }
        )
        if len(threads) >= THREAD_MAX:
            break
    return {"threads": threads}


def fill_daily(
    threads: dict[str, Any],
    scenes: list[dict[str, Any]],
    recent_count: int,
) -> dict[str, Any]:
    """没被任何线点到的场归到「日常」。最近几场留一行，更早的只留编号。"""
    linked: set[int] = set()
    for thread in threads.get("threads") or []:
        linked.update(int(scene_id) for scene_id in thread.get("scenes") or [])
    loose = [row for row in scenes if int(row["id"]) not in linked]
    loose.sort(key=lambda row: int(row["id"]), reverse=True)
    keep = max(0, int(recent_count))
    recent = [
        {"id": int(row["id"]), "line": _line(row)}
        for row in loose[:keep]
    ]
    older = [int(row["id"]) for row in loose[keep:]]
    return {"threads": list(threads.get("threads") or []), "daily": {"recent": recent, "older": older}}


def render(tree: dict[str, Any], unlinked: list[str]) -> str:
    lines = ["# 故事树"]
    threads = tree.get("threads") or []
    if not threads:
        lines.append("（还没有未了结的线）")
    for thread in threads:
        lines.append(f"## {thread.get('title')}")
        people = "、".join(thread.get("people") or [])
        canon = "、".join(thread.get("canon") or [])
        seed = "、".join(thread.get("seed") or [])
        scenes = " ".join(f"#{scene_id}" for scene_id in thread.get("scenes") or [])
        if people:
            lines.append(f"人：{people}")
        if canon:
            lines.append(f"设定：{canon}")
        if seed:
            lines.append(f"种子：{seed}")
        if scenes:
            lines.append(f"场：{scenes}")
    daily = tree.get("daily") if isinstance(tree.get("daily"), dict) else {}
    lines.append("## 日常")
    recent = daily.get("recent") or []
    older = daily.get("older") or []
    if not recent and not older:
        lines.append("（没有没挂上线的场）")
    for row in recent:
        if isinstance(row, dict):
            lines.append(f"- #{row.get('id')} {row.get('line') or ''}".rstrip())
    if older:
        lines.append("更早：" + " ".join(f"#{scene_id}" for scene_id in older))
    if unlinked:
        lines.append("未挂上的设定：" + "、".join(unlinked))
    lines.append("要看某一场或某条设定的正文，把编号或名字放进 lookup。")
    return "\n".join(lines)


def _names(value: Any, allowed: set[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        text = str(item or "").strip()[:NAME_MAX]
        if text and text in allowed and text not in names:
            names.append(text)
    return names


def _scenes(value: Any, known: set[int], current: int | None) -> list[int]:
    if not isinstance(value, list):
        return []
    ids: list[int] = []
    for item in value:
        if item in {0, "0", "这场"} and current:
            scene_id = int(current)
        else:
            try:
                scene_id = int(item)
            except (TypeError, ValueError):
                continue
        if scene_id in known and scene_id not in ids:
            ids.append(scene_id)
    return ids


def _line(row: dict[str, Any]) -> str:
    return " ".join(str(row.get("summary") or "").split())[:80]
