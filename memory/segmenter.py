"""话题分段器：观察消息、按空闲切会话、关闭事件并写入碎片与实体。"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from ..core.types import Turn
from . import prompts
from .dynamics import Dynamics
from .edges import EdgeRepo, normalize_entities

ConfigProvider = Callable[[str], ConfigView]


class Segmenter:
    def __init__(
        self,
        db: Database,
        configs: ConfigProvider,
        dynamics: Dynamics,
        edges: EdgeRepo,
        textgen: Any,
        audit: Audit,
    ) -> None:
        self.db = db
        self.configs = configs
        self.dynamics = dynamics
        self.edges = edges
        self.textgen = textgen
        self.audit = audit

    # ------------------------------------------------------------------ 观察
    async def observe(
        self, turn: Turn, *, display_name: str = "", chat_type: str = ""
    ) -> int | None:
        cfg = self.configs(turn.bot_id)
        if not cfg.bool("memory.enabled", True):
            return None
        await self._touch_session(turn, display_name, chat_type)

        idle_ms = cfg.int("memory.idle_split_minutes", 30, lo=1, hi=1440) * 60_000
        event = await self.db.fetchone(
            "SELECT * FROM events WHERE bot_id=? AND umo=? AND status='open' AND kind='episodic' "
            "ORDER BY id DESC LIMIT 1",
            (turn.bot_id, turn.umo),
        )
        if event is not None:
            last_ts = await self.db.scalar(
                "SELECT MAX(ts) FROM event_turns WHERE event_id=?", (event["id"],), default=0
            )
            if last_ts and int(turn.ts) - int(last_ts) > idle_ms:
                await self.close_event(int(event["id"]), reason="idle")
                event = None

        if event is None:
            event_id = await self.db.execute(
                "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, participants, "
                "turn_count, char_count, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (turn.bot_id, turn.umo, "episodic", turn.ts, turn.ts, "[]", 0, 0, "open", turn.ts),
            )
            participants: list[dict[str, str]] = []
            seq = 1
        else:
            event_id = int(event["id"])
            participants = self._load_participants(event.get("participants"))
            seq = int(
                await self.db.scalar(
                    "SELECT MAX(seq) FROM event_turns WHERE event_id=?", (event_id,), default=0
                )
                or 0
            ) + 1

        await self.db.execute(
            "INSERT INTO event_turns(event_id, seq, role, speaker_id, speaker_name, text, ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                event_id,
                seq,
                turn.role,
                turn.speaker_id,
                turn.speaker_name,
                turn.text,
                turn.ts,
            ),
        )
        if turn.role == "user" and turn.speaker_id:
            entry = {"id": turn.speaker_id, "name": turn.speaker_name}
            if all(item.get("id") != entry["id"] for item in participants):
                participants.append(entry)
        await self.db.execute(
            "UPDATE events SET turn_count=turn_count+1, char_count=char_count+?, "
            "ended_at=?, participants=? WHERE id=?",
            (len(turn.text or ""), turn.ts, json.dumps(participants, ensure_ascii=False), event_id),
        )
        return event_id

    async def _touch_session(self, turn: Turn, display_name: str, chat_type: str) -> None:
        summary = (turn.text or "")[:40] if turn.role == "user" else None
        await self.db.execute(
            """
            INSERT INTO sessions(bot_id, umo, name, type, last_message_at, last_summary)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(bot_id, umo) DO UPDATE SET
              name = COALESCE(NULLIF(excluded.name, ''), sessions.name),
              type = COALESCE(NULLIF(excluded.type, ''), sessions.type),
              last_message_at = excluded.last_message_at,
              last_summary = COALESCE(excluded.last_summary, sessions.last_summary)
            """,
            (turn.bot_id, turn.umo, display_name, chat_type, turn.ts, summary),
        )

    @staticmethod
    def _load_participants(raw: Any) -> list[dict[str, str]]:
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, str) and raw.strip():
            try:
                value = json.loads(raw)
            except (ValueError, TypeError):
                return []
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    # ------------------------------------------------------------------ 关闭
    @staticmethod
    def _peer_name(turns: list[dict[str, Any]]) -> str:
        for turn in reversed(turns):
            if turn.get("role") == "user":
                name = str(turn.get("speaker_name") or "").strip()
                if name:
                    return name[:12]
        return "对方"

    async def _generate_event_content(
        self,
        event: dict[str, Any],
        turns: list[dict[str, Any]],
        cfg: ConfigView,
    ) -> dict[str, Any]:
        bot_id = str(event["bot_id"])
        limit = cfg.int("memory.turn_context_turns", 30, lo=4, hi=200)
        transcript = self._transcript(turns[-limit:])
        big = (
            int(event.get("char_count") or 0) >= cfg.int("memory.big_event_chars", 20000)
            or int(event.get("turn_count") or 0) >= cfg.int("memory.big_event_turns", 80)
        )
        result = await self.textgen.generate_json(
            prompts.CLOSE_EVENT_SYSTEM,
            prompts.CLOSE_EVENT_USER.format(
                turns=len(turns),
                chars=int(event.get("char_count") or 0),
                transcript=transcript,
            ),
            bot_id=bot_id,
            umo=str(event.get("umo") or ""),
            schema_hint=prompts.CLOSE_EVENT_SCHEMA,
            task="fragment",
        )

        fragment = ""
        entities: list[str] = []
        summary = ""
        excerpts: list[str] = []
        if isinstance(result, dict):
            fragment = str(result.get("fragment") or "").strip()
            entities = normalize_entities(result.get("entities"))
            summary = str(result.get("summary") or "").strip()
            raw_excerpts = result.get("excerpts")
            if isinstance(raw_excerpts, list):
                excerpts = [str(item)[:200] for item in raw_excerpts if str(item).strip()][:5]

        if not fragment:
            name = self._peer_name(turns)
            last_user = next(
                (str(turn["text"]) for turn in reversed(turns) if turn["role"] == "user"), ""
            )
            if last_user:
                prefix = f"{name}说："
                fragment = prefix + last_user[: max(1, 60 - len(prefix))]
            else:
                fragment = f"{name}说了一些事"
            self.audit.warn("事件 %s 碎片生成失败，使用归属兜底", event.get("id"))
        if big and not summary:
            summary = transcript[:300]
        return {
            "fragment": fragment,
            "entities": entities,
            "summary": summary,
            "excerpts": excerpts,
            "big": big,
        }

    async def close_event(self, event_id: int, *, reason: str = "topic_end") -> dict[str, Any] | None:
        event = await self.db.fetchone("SELECT * FROM events WHERE id=?", (event_id,))
        if event is None or event.get("status") != "open":
            return None
        bot_id = str(event["bot_id"])
        cfg = self.configs(bot_id)
        turns = await self.db.fetchall(
            "SELECT role, speaker_name, text, ts FROM event_turns WHERE event_id=? ORDER BY seq",
            (event_id,),
        )
        if not turns:
            await self.db.execute("DELETE FROM events WHERE id=?", (event_id,))
            return None

        content = await self._generate_event_content(event, turns, cfg)
        fragment = str(content["fragment"])
        entities = list(content["entities"])
        summary = str(content["summary"])
        excerpts = list(content["excerpts"])
        big = bool(content["big"])

        salience = int(event.get("salience") or 2)
        salience = min(5, max(1, salience))
        strength = self.dynamics.s0_event(salience)
        now = int(turns[-1]["ts"])
        await self.db.execute(
            "UPDATE events SET status='closed', fragment=?, summary=?, excerpts=?, entities=?, "
            "storage_strength=?, retrievability=1.0, last_recalled_at=?, ended_at=?, truncated=?, "
            "salience=? WHERE id=?",
            (
                fragment,
                summary or None,
                json.dumps(excerpts, ensure_ascii=False) if excerpts else None,
                json.dumps(entities, ensure_ascii=False),
                strength,
                now,
                now,
                1 if big else 0,
                salience,
                event_id,
            ),
        )
        if cfg.bool("edges.enabled", True) and entities:
            try:
                await self.edges.link(
                    bot_id,
                    event_id,
                    entities,
                    now,
                    max_degree=cfg.int("edges.max_degree", 20, lo=1, hi=200),
                )
            except Exception as exc:  # noqa: BLE001 - 建边失败不阻断事件写入
                self.audit.warn("事件 %s 建边失败：%s", event_id, exc)
        self.audit.event(
            "event_closed",
            bot_id=bot_id,
            event_id=event_id,
            reason=reason,
            turns=len(turns),
            chars=int(event.get("char_count") or 0),
            entities=len(entities),
            salience=salience,
            s0=round(strength, 3),
        )
        return {"event_id": event_id, "fragment": fragment, "entities": entities}

    async def rebuild_event(
        self, event_id: int, *, expected_bot_id: str = ""
    ) -> dict[str, Any]:
        """按原始 turns 重建一条已关闭事件的文本，保留 S/R 与状态。"""
        event = await self.db.fetchone("SELECT * FROM events WHERE id=?", (event_id,))
        if event is None:
            raise ValueError("事件不存在")
        bot_id = str(event["bot_id"])
        if expected_bot_id and bot_id != expected_bot_id:
            raise ValueError("事件不属于这个机器人")
        if event.get("status") != "closed":
            raise ValueError("只有已关闭的事件可以重建")
        turns = await self.db.fetchall(
            "SELECT role, speaker_name, text, ts FROM event_turns WHERE event_id=? ORDER BY seq",
            (event_id,),
        )
        if not turns:
            raise ValueError("这条事件没有原始对话，无法重建")
        cfg = self.configs(bot_id)
        content = await self._generate_event_content(event, turns, cfg)
        fragment = str(content["fragment"])
        summary = str(content["summary"])
        entities = list(content["entities"])
        excerpts = list(content["excerpts"])
        old_fragment = str(event.get("fragment") or "")
        await self.db.execute(
            "UPDATE events SET fragment=?, summary=?, excerpts=?, entities=? WHERE id=?",
            (
                fragment,
                summary or None,
                json.dumps(excerpts, ensure_ascii=False) if excerpts else None,
                json.dumps(entities, ensure_ascii=False),
                event_id,
            ),
        )
        self.audit.event(
            "event_rebuilt",
            bot_id=bot_id,
            event_id=event_id,
            old_chars=len(old_fragment),
            new_chars=len(fragment),
            entities=len(entities),
        )
        return {
            "event_id": event_id,
            "fragment": fragment,
            "summary": summary,
            "entities": entities,
        }

    async def close_idle(self, now: int) -> int:
        rows = await self.db.fetchall(
            "SELECT e.id, e.bot_id, MAX(t.ts) AS last_ts FROM events e "
            "JOIN event_turns t ON t.event_id = e.id "
            "WHERE e.status='open' AND e.kind='episodic' GROUP BY e.id"
        )
        closed = 0
        for row in rows:
            cfg = self.configs(str(row["bot_id"]))
            idle_ms = cfg.int("memory.idle_split_minutes", 30, lo=1, hi=1440) * 60_000
            last_ts = int(row.get("last_ts") or now)
            if now - last_ts >= idle_ms:
                if await self.close_event(int(row["id"]), reason="idle_sweep"):
                    closed += 1
        return closed

    async def maybe_close_after_turn(
        self, bot_id: str, umo: str, reply_text: str, topic_end: float | None
    ) -> bool:
        cfg = self.configs(bot_id)
        threshold = cfg.float("memory.topic_end_threshold", 0.7, lo=0.0, hi=1.0)
        if topic_end is None or topic_end < threshold:
            return False
        if self.ends_with_question(reply_text):
            return False
        event = await self.db.fetchone(
            "SELECT id FROM events WHERE bot_id=? AND umo=? AND status='open' AND kind='episodic' "
            "ORDER BY id DESC LIMIT 1",
            (bot_id, umo),
        )
        if event is None:
            return False
        return bool(await self.close_event(int(event["id"]), reason="topic_end"))

    @staticmethod
    def ends_with_question(text: str | None) -> bool:
        from .textutil import ends_with_question

        return ends_with_question(text)

    @staticmethod
    def _transcript(turns: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for turn in turns:
            role = str(turn.get("role"))
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            if role == "assistant":
                lines.append(f"我: {text}")
            else:
                name = str(turn.get("speaker_name") or "对方")
                lines.append(f"{name}: {text}")
        return "\n".join(lines)
