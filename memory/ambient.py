"""常驻背景记忆：近期事件摘要、认识的人、重要记忆。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from .textutil import trim_at_boundary

IMPORTANCE_INSTRUCTION = (
    "Is this event important enough to remain in the background of my everyday life—"
    "something about who I am, a relationship, or a lasting fact—rather than ordinary "
    "conversation? Prefer no unless it should always be present."
)


@dataclass
class AmbientSnapshot:
    text: str = ""
    recent: list[dict[str, Any]] = field(default_factory=list)
    people: list[dict[str, Any]] = field(default_factory=list)
    important: list[dict[str, Any]] = field(default_factory=list)
    recent_event_ids: list[int] = field(default_factory=list)
    important_event_ids: list[int] = field(default_factory=list)


class AmbientService:
    def __init__(
        self,
        db: Database,
        configs: Callable[[str], ConfigView],
        jev: Any,
        audit: Audit,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.jev = jev
        self.audit = audit
        self.now_fn = now_fn
        self._backfilled: set[str] = set()

    async def snapshot(self, bot_id: str, now: int | None = None) -> AmbientSnapshot:
        cfg = self.configs(bot_id)
        if not cfg.bool("ambient.enabled", True):
            return AmbientSnapshot()
        stamp = now or self.now_fn()
        await self._ensure_backfill(bot_id)

        important = await self.db.fetchall(
            "SELECT text, source_event_id, importance FROM ambient_memories "
            "WHERE bot_id=? AND kind='important' AND active=1 "
            "ORDER BY importance DESC, updated_at DESC LIMIT ?",
            (bot_id, cfg.int("ambient.important_limit", 5, lo=1, hi=20)),
        )
        important_ids = {
            int(row["source_event_id"])
            for row in important
            if row.get("source_event_id") is not None
        }
        recent = await self._recent(bot_id, cfg, stamp, important_ids)
        people = await self.db.fetchall(
            "SELECT person_id, name, aliases, last_topic, last_seen FROM people "
            "WHERE bot_id=? ORDER BY last_seen DESC LIMIT ?",
            (bot_id, cfg.int("ambient.people_limit", 5, lo=1, hi=20)),
        )
        text = self._render(cfg, recent, people, important)
        return AmbientSnapshot(
            text=text,
            recent=recent,
            people=people,
            important=important,
            recent_event_ids=[int(row["id"]) for row in recent],
            important_event_ids=sorted(important_ids),
        )

    async def _recent(
        self, bot_id: str, cfg: ConfigView, now: int, important_ids: set[int]
    ) -> list[dict[str, Any]]:
        """最近的事：不收无足轻重的（salience 1），并给聊天留名额。

        故事模块每天会产生很多事件；不留名额的话，“最近”会全是她自己的日常，
        和人聊过什么反而从背景里消失。
        """
        limit = cfg.int("ambient.recent_limit", 5, lo=1, hi=20)
        chat_min = min(limit, cfg.int("ambient.recent_chat_min", 2, lo=0, hi=20))
        since = now - cfg.int("ambient.recent_days", 7, lo=1, hi=60) * 86_400_000
        base = (
            "SELECT id, fragment, summary, ended_at, source FROM events "
            "WHERE bot_id=? AND status='closed' AND merged_into IS NULL "
            "AND kind IN ('episodic', 'gist') AND ended_at >= ? "
            "AND COALESCE(salience, 2) > 1 "
        )
        fetch = limit + len(important_ids)
        rows = await self.db.fetchall(
            base + "ORDER BY ended_at DESC, id DESC LIMIT ?", (bot_id, since, fetch)
        )
        recent = [row for row in rows if int(row["id"]) not in important_ids][:limit]
        missing = chat_min - sum(1 for row in recent if row.get("source") != "story")
        if missing > 0:
            chat_rows = await self.db.fetchall(
                base + "AND source<>'story' ORDER BY ended_at DESC, id DESC LIMIT ?",
                (bot_id, since, chat_min + len(important_ids)),
            )
            chosen = {int(row["id"]) for row in recent}
            extra = [
                row
                for row in chat_rows
                if int(row["id"]) not in important_ids and int(row["id"]) not in chosen
            ][:missing]
            # 让出最旧的故事名额，再按时间排回去
            overflow = len(recent) + len(extra) - limit
            for index in range(len(recent) - 1, -1, -1):
                if overflow <= 0:
                    break
                if recent[index].get("source") == "story":
                    del recent[index]
                    overflow -= 1
            recent = sorted(
                recent + extra,
                key=lambda row: (int(row.get("ended_at") or 0), int(row["id"])),
                reverse=True,
            )[:limit]
        return recent

    async def on_event_closed(self, event: dict[str, Any]) -> None:
        bot_id = str(event.get("bot_id") or "")
        if not bot_id:
            return
        cfg = self.configs(bot_id)
        if not cfg.bool("ambient.enabled", True):
            return
        turns = list(event.get("turns") or [])
        fragment = str(event.get("fragment") or "").strip()
        summary = str(event.get("summary") or "").strip()
        topic = self._topic(fragment or summary, cfg.int("ambient.person_topic_chars", 20))
        now = int(event.get("now") or self.now_fn())
        seen: set[str] = set()
        for turn in turns:
            if turn.get("role") != "user":
                continue
            person_id = str(turn.get("speaker_id") or "").strip()
            name = str(turn.get("speaker_name") or "").strip()
            if not person_id and not name:
                continue
            person_id = person_id or name
            if person_id in seen:
                continue
            seen.add(person_id)
            await self._upsert_person(
                bot_id, person_id, name or "对方", topic, now
            )

        salience = int(event.get("salience") or 0)
        if salience < cfg.int("ambient.min_salience", 3, lo=1, hi=5):
            return
        if not getattr(self.jev, "available", False):
            return
        try:
            probability = await self.jev.noul(
                {
                    "event": fragment,
                    "summary": summary,
                    "salience": salience,
                },
                IMPORTANCE_INSTRUCTION,
            )
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("重要记忆判断失败：%s", exc)
            return
        if probability is None or float(probability) < cfg.float(
            "ambient.promote_threshold", 0.75, lo=0.0, hi=1.0
        ):
            return
        await self._promote(
            bot_id,
            event_id=int(event.get("event_id") or 0),
            text=self._topic(fragment or summary, cfg.int("ambient.important_chars", 20)),
            importance=float(probability),
            now=now,
            limit=cfg.int("ambient.important_limit", 5, lo=1, hi=20),
        )

    async def _ensure_backfill(self, bot_id: str) -> None:
        if bot_id in self._backfilled:
            return
        self._backfilled.add(bot_id)
        # 只回填还不在 people 里的人，每人只取最近的一条发言，筛选都在 SQL 里做完。
        # 以前是把这个机器人全部用户发言读进内存再去重，数据多了重启后第一条回复会很慢。
        # （GROUP BY 配 MAX(ts) 时，SQLite 会让其余列取自 ts 最大的那一行。）
        try:
            rows = await self.db.fetchall(
                "SELECT pid, speaker_name, MAX(ts) AS ts, fragment, summary FROM ("
                "  SELECT COALESCE(NULLIF(TRIM(COALESCE(t.speaker_id, '')), ''), "
                "                  NULLIF(TRIM(COALESCE(t.speaker_name, '')), '')) AS pid, "
                "         t.speaker_name, t.ts, e.fragment, e.summary "
                "  FROM event_turns t JOIN events e ON e.id=t.event_id "
                "  WHERE e.bot_id=? AND e.status='closed' AND t.role='user'"
                ") WHERE pid IS NOT NULL "
                "AND pid NOT IN (SELECT person_id FROM people WHERE bot_id=?) "
                "GROUP BY pid",
                (bot_id, bot_id),
            )
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("常驻人物回填失败：%s", exc)
            return
        for row in rows:
            name = str(row.get("speaker_name") or "").strip()
            topic = self._topic(
                str(row.get("fragment") or row.get("summary") or ""),
                20,
            )
            await self._upsert_person(
                bot_id, str(row["pid"]), name or "对方", topic, int(row.get("ts") or 0)
            )

    async def _upsert_person(
        self, bot_id: str, person_id: str, name: str, topic: str, now: int
    ) -> None:
        old = await self.db.fetchone(
            "SELECT name, aliases FROM people WHERE bot_id=? AND person_id=?",
            (bot_id, person_id),
        )
        aliases: list[str] = []
        if old:
            try:
                parsed = json.loads(str(old.get("aliases") or "[]"))
                if isinstance(parsed, list):
                    aliases = [str(item) for item in parsed if str(item).strip()]
            except (TypeError, ValueError):
                aliases = []
            old_name = str(old.get("name") or "").strip()
            if old_name and old_name != name and old_name not in aliases:
                aliases.append(old_name)
        await self.db.execute(
            "INSERT INTO people(bot_id, person_id, name, aliases, last_topic, last_seen, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(bot_id, person_id) DO UPDATE SET "
            "name=excluded.name, aliases=excluded.aliases, last_topic=excluded.last_topic, "
            "last_seen=MAX(people.last_seen, excluded.last_seen), updated_at=excluded.updated_at",
            (
                bot_id,
                person_id,
                name,
                json.dumps(aliases, ensure_ascii=False),
                topic or None,
                now,
                now,
                now,
            ),
        )

    async def _promote(
        self,
        bot_id: str,
        *,
        event_id: int,
        text: str,
        importance: float,
        now: int,
        limit: int,
    ) -> None:
        if not text or event_id <= 0:
            return
        exists = await self.db.fetchone(
            "SELECT id FROM ambient_memories WHERE bot_id=? AND kind='important' "
            "AND source_event_id=? AND active=1",
            (bot_id, event_id),
        )
        if exists:
            return
        await self.db.execute(
            "INSERT INTO ambient_memories(bot_id, kind, text, source_event_id, importance, "
            "active, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (bot_id, "important", text, event_id, importance, 1, now, now),
        )
        rows = await self.db.fetchall(
            "SELECT id FROM ambient_memories WHERE bot_id=? AND kind='important' AND active=1 "
            "ORDER BY importance DESC, updated_at DESC, id DESC",
            (bot_id,),
        )
        stale = [int(row["id"]) for row in rows[limit:]]
        for memory_id in stale:
            await self.db.execute(
                "UPDATE ambient_memories SET active=0, updated_at=? WHERE id=?",
                (now, memory_id),
            )

    @staticmethod
    def _topic(text: str, limit: int) -> str:
        body = " ".join(str(text or "").split())
        return trim_at_boundary(body, max(1, int(limit))).strip()

    def _render(
        self,
        cfg: ConfigView,
        recent: list[dict[str, Any]],
        people: list[dict[str, Any]],
        important: list[dict[str, Any]],
    ) -> str:
        lines = ["# 我这阵子的背景（不用主动提，只影响语气/判断）"]
        recent_text = "；".join(
            self._topic(str(row.get("summary") or row.get("fragment") or ""), 60)
            for row in recent
            if str(row.get("summary") or row.get("fragment") or "").strip()
        )
        if recent_text:
            recent_text = trim_at_boundary(
                recent_text,
                cfg.int("ambient.recent_max_chars", 150, lo=50, hi=400),
            )
            lines.append(f"- 最近：{recent_text}")
        people_text = "；".join(
            self._person_line(row, cfg.int("ambient.person_topic_chars", 20))
            for row in people
        )
        if people_text:
            lines.append(f"- 认识的人：{people_text}")
        important_text = "；".join(
            self._topic(str(row.get("text") or ""), cfg.int("ambient.important_chars", 20))
            for row in important
            if str(row.get("text") or "").strip()
        )
        if important_text:
            lines.append(f"- 重要：{important_text}")
        if len(lines) == 1:
            return ""
        return trim_at_boundary(
            "\n".join(lines),
            cfg.int("ambient.total_max_chars", 400, lo=100, hi=1200),
        )

    def _person_line(self, row: dict[str, Any], topic_chars: int) -> str:
        name = self._topic(str(row.get("name") or "对方"), 8)
        topic = self._topic(str(row.get("last_topic") or ""), topic_chars)
        line = f"{name}（{topic}）" if topic else name
        return trim_at_boundary(line, 20)
