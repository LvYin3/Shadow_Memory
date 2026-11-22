"""一次唤醒的编排：开场（含补叙）→ 多轮往返与裁定 → 收场 → 进入记忆。

背景模型是主持人：叙述她能感知的情境、裁定结果。她是玩家：只决定自己的想法和行动。
一场要么完整落库（记忆事件、状态卡、设定集、笔记、下次预约、扣额度），要么整场丢弃。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from ..memory import prompts as memory_prompts
from ..memory.edges import normalize_entities
from ..memory.service import MemoryService
from . import bucket, oracle, prompts, world
from .card import MINUTE_MS, StatusCard, build_card, load_card, render_for_gm
from .llm import ROLE_ACTOR, ROLE_GM, StoryLLM, StoryLLMError
from .store import StoryStore, story_umo

ConfigProvider = Callable[[str], ConfigView]
# bot_id -> (基础人设文本, 来源说明)
BaseCardResolver = Callable[[str], Awaitable[tuple[str, str]]]

NARRATOR_NAME = "旁白"


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


@dataclass(slots=True)
class SceneProgress:
    """一场进行到哪了；失败时也靠它留下排障用的记录。"""

    started_at: int
    cause: str
    calls: int = 0
    rounds: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)
    rolls: list[dict[str, Any]] = field(default_factory=list)
    seed: dict[str, Any] = field(default_factory=dict)
    backfill: str = ""


@dataclass(slots=True)
class SceneResult:
    scene_id: int
    event_id: int
    backfill_event_id: int | None
    rounds: int
    calls: int
    salience: int
    fragment: str
    next_wake_at: int
    tokens: float
    card: StatusCard


class SceneFailed(Exception):
    def __init__(self, message: str, progress: SceneProgress | None = None) -> None:
        super().__init__(message)
        self.progress = progress


def _valid_opening(value: dict[str, Any]) -> bool:
    return bool(str(value.get("narration") or "").strip())


def _valid_action(value: dict[str, Any]) -> bool:
    return bool(str(value.get("action") or "").strip())


def _valid_turn(value: dict[str, Any]) -> bool:
    check = value.get("check")
    if isinstance(check, dict):
        return bool(str(check.get("success") or "").strip()) and bool(
            str(check.get("failure") or "").strip()
        )
    return bool(str(value.get("narration") or "").strip())


def _valid_closing(value: dict[str, Any]) -> bool:
    card = value.get("card")
    return isinstance(card, dict) and bool(
        str(card.get("where") or "").strip() or str(card.get("doing") or "").strip()
    )


class Director:
    def __init__(
        self,
        *,
        db: Database,
        configs: ConfigProvider,
        store: StoryStore,
        memory: MemoryService,
        llm: StoryLLM,
        audit: Audit,
        base_card: BaseCardResolver,
        rng: random.Random | None = None,
        now_fn: Callable[[], int] = clock_mod.now_ms,
    ) -> None:
        self.db = db
        self.configs = configs
        self.store = store
        self.memory = memory
        self.llm = llm
        self.audit = audit
        self._base_card = base_card
        self.rng = rng or random.Random()
        self.now_fn = now_fn

    # ------------------------------------------------------------------ 入口
    async def run(self, bot_id: str, *, manual: bool = False) -> SceneResult:
        progress = SceneProgress(
            started_at=self.now_fn(), cause="manual" if manual else "scheduled"
        )
        state = await self.store.state(bot_id)
        if state is None or not str(state.get("outline") or "").strip():
            raise SceneFailed("还没有导入世界种子", progress)
        base_card, _ = await self._base_card(bot_id)
        if not base_card.strip():
            raise SceneFailed("没有取到她的基础人设", progress)
        try:
            return await self._play(bot_id, state, base_card, progress, manual)
        except StoryLLMError as exc:
            progress.calls += exc.attempts
            raise SceneFailed(str(exc), progress) from exc

    # ------------------------------------------------------------------ 场景
    async def _play(
        self,
        bot_id: str,
        state: dict[str, Any],
        base_card: str,
        progress: SceneProgress,
        manual: bool,
    ) -> SceneResult:
        cfg = self.configs(bot_id)
        started = progress.started_at
        per_day = cfg.float("story.refill_per_day", 10, lo=0, hi=1000)
        capacity = cfg.float("story.bucket_capacity", 30, lo=1, hi=10000)
        min_wake = cfg.int("story.min_wake_minutes", 10, lo=1, hi=1440)
        max_wake = max(min_wake, cfg.int("story.max_wake_minutes", 720, lo=1, hi=10080))
        max_rounds = cfg.int("story.max_rounds", 4, lo=1, hi=8)

        last_scene_at = int(state.get("last_scene_at") or 0)
        first = not last_scene_at
        gap_ms = None if first else max(0, started - last_scene_at)
        backfill_floor = cfg.int("story.backfill_min_minutes", 30, lo=1, hi=1440) * MINUTE_MS
        want_backfill = gap_ms is not None and gap_ms >= backfill_floor

        progress.seed = oracle.draw_seed(self.rng).as_dict()
        moment = oracle.real_moment(started)
        canon = await self.store.canon(bot_id)
        context = await self._gm_context(
            bot_id,
            cfg,
            state,
            canon,
            now=started,
            gap_ms=gap_ms,
            moment=moment["text"],
            seed=progress.seed,
            tokens=bucket.refill(
                float(state.get("tokens") or 0.0),
                int(state.get("tokens_at") or 0),
                started,
                per_day=per_day,
                capacity=capacity,
            ),
            per_day=per_day,
            capacity=capacity,
        )

        # ---- 开场（含补叙）
        opening, used = await self.llm.ask_json(
            ROLE_GM,
            bot_id,
            prompts.GM_SYSTEM,
            context + "\n\n" + prompts.gm_open_task(first=first, backfill=want_backfill),
            validate=_valid_opening,
        )
        progress.calls += used
        progress.backfill = _clip(opening.get("backfill"), 300) if want_backfill else ""
        progress.transcript.append({"role": "narrator", "text": _clip(opening["narration"], 400)})
        lookups = self._lookups(canon, opening.get("lookup"))

        # ---- 她的上下文：人设、人格、常驻背景每轮都带；召回只在开场做一次
        prepared = await self.memory.prepare_turn(
            bot_id, story_umo(bot_id), progress.transcript[0]["text"], [], started
        )
        if prepared.injection is not None and prepared.injection.candidates > 0:
            progress.calls += 1
        actor_system = prompts.actor_system(
            base_card=base_card,
            persona=prepared.persona_text,
            ambient=prepared.ambient_text,
            moment=moment["text"],
        )
        recall_parts = list(prepared.parts)

        # ---- 多轮往返
        twist = ""
        for round_no in range(1, max_rounds + 1):
            seen = (
                [{"role": "narrator", "text": progress.backfill}] if progress.backfill else []
            ) + progress.transcript
            action, used = await self.llm.ask_json(
                ROLE_ACTOR,
                bot_id,
                actor_system,
                prompts.actor_user(seen, recall_parts),
                validate=_valid_action,
            )
            progress.calls += used
            progress.transcript.append(
                {
                    "role": "me",
                    "inner": _clip(action.get("inner"), 120),
                    "text": _clip(action["action"], 240),
                }
            )
            progress.rounds = round_no

            last = round_no >= max_rounds
            turn, used = await self.llm.ask_json(
                ROLE_GM,
                bot_id,
                prompts.GM_SYSTEM,
                "\n\n".join(
                    [
                        context,
                        prompts.gm_progress(
                            progress.transcript,
                            backfill=progress.backfill,
                            twist=twist,
                            lookups=lookups,
                        ),
                        prompts.gm_turn_task(last=last),
                    ]
                ),
                validate=_valid_turn,
            )
            progress.calls += used
            lookups = self._lookups(canon, turn.get("lookup"))
            twist = ""
            check = turn.get("check")
            if isinstance(check, dict):
                # 成败由骰子定：背景模型事先写好两种后续，看不到骰值，没法事后挑档位。
                result = oracle.roll(check.get("likelihood"), self.rng)
                narration = check["success"] if result.success else check["failure"]
                progress.rolls.append(
                    {**result.as_dict(), "round": round_no, "what": _clip(check.get("what"), 80)}
                )
                if result.twist:
                    twist = result.label
            else:
                narration = turn["narration"]
            progress.transcript.append({"role": "narrator", "text": _clip(narration, 400)})
            if last or bool(turn.get("end")):
                break

        # ---- 收场
        closing, used = await self.llm.ask_json(
            ROLE_GM,
            bot_id,
            prompts.GM_SYSTEM,
            "\n\n".join(
                [
                    context,
                    prompts.gm_progress(
                        progress.transcript,
                        backfill=progress.backfill,
                        twist=twist,
                        lookups=lookups,
                    ),
                    prompts.gm_close_task(min_wake=min_wake, max_wake=max_wake),
                ]
            ),
            validate=_valid_closing,
        )
        progress.calls += used
        epilogue = _clip(closing.get("epilogue"), 200)
        if epilogue:
            progress.transcript.append({"role": "narrator", "text": epilogue})

        memory = await self._remember(bot_id, cfg, progress)
        return await self._commit(
            bot_id,
            cfg,
            state,
            progress,
            closing,
            memory,
            manual=manual,
            per_day=per_day,
            capacity=capacity,
            min_wake=min_wake,
            max_wake=max_wake,
        )

    # ------------------------------------------------------------------ 背景模型的上下文
    @staticmethod
    def _lookups(canon: list[dict[str, Any]], names: Any) -> list[str]:
        if not isinstance(names, list):
            return []
        return [world.render_entry(entry) for entry in world.find_by_names(canon, names)]

    async def _gm_context(
        self,
        bot_id: str,
        cfg: ConfigView,
        state: dict[str, Any],
        canon: list[dict[str, Any]],
        *,
        now: int,
        gap_ms: int | None,
        moment: str,
        seed: dict[str, Any],
        tokens: float,
        per_day: float,
        capacity: float,
    ) -> str:
        meta = self.store.seed_meta(state)
        opening = meta.get("opening") if isinstance(meta.get("opening"), dict) else {}
        card = load_card(state.get("card"))

        scenes = await self.store.recent_scenes(
            bot_id, cfg.int("story.recent_scenes", 3, lo=0, hi=20)
        )
        scene_lines = [
            f"{clock_mod.iso(int(row['started_at']))[5:16].replace('T', ' ')} {row.get('summary')}"
            for row in reversed(scenes)
            if str(row.get("summary") or "").strip()
        ]

        # 只读外显：聊天事件的碎片与摘要。不读人格三层、洞察和私人笔记。
        last_scene_at = int(state.get("last_scene_at") or 0)
        digest = await self.store.chat_digest(
            bot_id,
            last_scene_at or now - 86_400_000,
            cfg.int("story.chat_digest_items", 5, lo=0, hi=20),
        )

        # 先查再编：按她此刻的处境预取相关设定，其余只给名字。
        query = " ".join(
            [
                card.where if card else "",
                card.doing if card else "",
                str(opening.get("where") or ""),
                str(opening.get("doing") or ""),
                *scene_lines,
                *digest,
            ]
        )
        limit = cfg.int("story.canon_context_items", 8, lo=0, hi=40)
        picked = world.search(canon, query, limit) or canon[:limit]
        picked_ids = {id(entry) for entry in picked}
        index = [str(entry["name"]) for entry in canon if id(entry) not in picked_ids][:80]

        budget = cfg.int("story.notes_max_chars", 2000, lo=0, hi=20000)
        notes: list[dict[str, Any]] = []
        all_notes = await self.store.notes(bot_id)
        used = 0
        for note in all_notes:
            size = len(str(note.get("title") or "")) + len(str(note.get("body") or ""))
            if notes and used + size > budget:
                break
            if not notes and budget <= 0:
                break
            notes.append(note)
            used += size

        return prompts.gm_context(
            moment=moment,
            gap_ms=gap_ms,
            tokens=tokens,
            per_day=per_day,
            capacity=capacity,
            title=str(state.get("world_title") or ""),
            outline=str(state.get("outline") or ""),
            hooks=[str(hook) for hook in meta.get("hooks") or []],
            opening={key: str(value) for key, value in opening.items()},
            canon_lines=[world.render_entry(entry) for entry in picked],
            canon_index=index,
            notes=notes,
            hidden_notes=len(all_notes) - len(notes),
            scenes=scene_lines,
            card=render_for_gm(card, now),
            digest=digest,
            seed=seed,
        )

    # ------------------------------------------------------------------ 进入记忆
    async def _remember(
        self, bot_id: str, cfg: ConfigView, progress: SceneProgress
    ) -> dict[str, Any]:
        """把场景整理成她的第一人称记忆，并评显著度。整理失败有兜底，不让整场作废。"""
        transcript = prompts.render_transcript(progress.transcript, viewer="memory")
        result = None
        try:
            result = await self.memory.textgen.generate_json(
                memory_prompts.STORY_EVENT_SYSTEM,
                memory_prompts.STORY_EVENT_USER.format(
                    backfill_block=memory_prompts.STORY_BACKFILL_BLOCK.format(
                        backfill=progress.backfill
                    )
                    if progress.backfill
                    else "",
                    transcript=transcript,
                    backfill_hint=memory_prompts.STORY_BACKFILL_HINT
                    if progress.backfill
                    else memory_prompts.STORY_NO_BACKFILL_HINT,
                ),
                bot_id=bot_id,
                umo=story_umo(bot_id),
                schema_hint=memory_prompts.STORY_EVENT_SCHEMA,
                task="fragment",
            )
        except Exception as exc:  # noqa: BLE001 - 写作模型异常按整理失败处理
            self.audit.warn("故事场景整理失败：%s", exc)
        progress.calls += 1

        fragment = ""
        summary = ""
        entities: list[str] = []
        backfill_fragment = ""
        if isinstance(result, dict):
            fragment = _clip(result.get("fragment"), 80)
            summary = _clip(result.get("summary"), 300)
            entities = normalize_entities(result.get("entities"))
            backfill_fragment = _clip(result.get("backfill_fragment"), 60)
        if not fragment:
            # 兜底：退回她最后做的事（她的行动本来就是第一人称写的）
            fragment = next(
                (
                    _clip(turn.get("text"), 60)
                    for turn in reversed(progress.transcript)
                    if turn.get("role") == "me"
                ),
                "",
            ) or "我过了一段平常的时间"
            self.audit.warn("故事场景碎片生成失败，使用兜底（bot=%s）", bot_id)
        if not progress.backfill:
            backfill_fragment = ""

        salience = 2
        jev = self.memory.jev
        if getattr(jev, "available", False):
            score = None
            try:
                score = await jev.score(
                    {
                        "me": (await self.memory.persona.render(bot_id))[:800],
                        "what_happened": transcript[:1500],
                        "my_memory": fragment,
                    },
                    memory_prompts.STORY_SALIENCE_INSTRUCTION,
                    memory_prompts.STORY_SALIENCE_LEVELS,
                )
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("故事场景显著度判断失败：%s", exc)
            progress.calls += 1
            if score is not None:
                salience = min(5, max(1, int(round(float(score))) + 1))
            if salience >= cfg.int("ambient.min_salience", 3, lo=1, hi=5) and cfg.bool(
                "ambient.enabled", True
            ):
                progress.calls += 1  # 提交后还会问一次“要不要进常驻背景”
        return {
            "fragment": fragment,
            "summary": summary,
            "entities": entities,
            "backfill_fragment": backfill_fragment,
            "salience": salience,
        }

    async def _insert_event(
        self,
        bot_id: str,
        *,
        started_at: int,
        ended_at: int,
        turns: list[tuple[str, str]],
        fragment: str,
        summary: str,
        entities: list[str],
        salience: int,
    ) -> int:
        """以已关闭状态直接写入，不经过分段器；来源标记为 story，其余与聊天事件同权。"""
        event_id = await self.db.execute(
            "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, participants, "
            "turn_count, char_count, fragment, summary, entities, storage_strength, "
            "retrievability, last_recalled_at, salience, status, source, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                bot_id,
                story_umo(bot_id),
                "episodic",
                started_at,
                ended_at,
                "[]",
                len(turns),
                sum(len(text) for _, text in turns),
                fragment,
                summary or None,
                json.dumps(entities, ensure_ascii=False),
                self.memory.dynamics.s0_event(salience),
                1.0,
                ended_at,
                salience,
                "closed",
                "story",
                ended_at,
            ),
        )
        for index, (role, text) in enumerate(turns, start=1):
            await self.db.execute(
                "INSERT INTO event_turns(event_id, seq, role, speaker_id, speaker_name, text, ts) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    event_id,
                    index,
                    role,
                    "",
                    NARRATOR_NAME if role == "narrator" else "",
                    text,
                    started_at + index,
                ),
            )
        return event_id

    async def _commit(
        self,
        bot_id: str,
        cfg: ConfigView,
        state: dict[str, Any],
        progress: SceneProgress,
        closing: dict[str, Any],
        memory: dict[str, Any],
        *,
        manual: bool,
        per_day: float,
        capacity: float,
        min_wake: int,
        max_wake: int,
    ) -> SceneResult:
        ended = max(self.now_fn(), progress.started_at)
        card = build_card(closing.get("card"), ended, max_minutes=max_wake)
        assert card is not None  # _valid_closing 已保证
        try:
            wake_minutes = int(float(closing.get("next_wake_minutes") or 0))
        except (TypeError, ValueError):
            wake_minutes = 0
        if wake_minutes <= 0:
            wake_minutes = max_wake
        wake_minutes = min(max(wake_minutes, min_wake), max_wake)
        next_wake_at = ended + wake_minutes * MINUTE_MS

        tokens = bucket.refill(
            float(state.get("tokens") or 0.0),
            int(state.get("tokens_at") or 0),
            ended,
            per_day=per_day,
            capacity=capacity,
        )
        if not manual:
            tokens = max(0.0, tokens - 1.0)

        turns: list[tuple[str, str]] = []
        for turn in progress.transcript:
            text = str(turn.get("text") or "")
            if turn.get("role") == "narrator":
                turns.append(("narrator", text))
            else:
                inner = str(turn.get("inner") or "")
                turns.append(("assistant", f"（心里：{inner}）{text}" if inner else text))

        salience = int(memory["salience"])
        last_scene_at = int(state.get("last_scene_at") or 0)
        # 事务里的读写都由当前任务顺序发出，不派生子任务。
        async with self.db.transaction():
            backfill_event_id = None
            if progress.backfill and memory["backfill_fragment"]:
                backfill_event_id = await self._insert_event(
                    bot_id,
                    started_at=last_scene_at or progress.started_at,
                    ended_at=progress.started_at,
                    turns=[("narrator", progress.backfill)],
                    fragment=memory["backfill_fragment"],
                    summary="",
                    entities=[],
                    salience=1,  # 补叙固定最低显著度
                )
            event_id = await self._insert_event(
                bot_id,
                started_at=progress.started_at,
                ended_at=ended,
                turns=turns,
                fragment=memory["fragment"],
                summary=memory["summary"],
                entities=memory["entities"],
                salience=salience,
            )
            scene_id = await self.store.insert_scene(
                bot_id,
                cause=progress.cause,
                status="ok",
                started_at=progress.started_at,
                ended_at=ended,
                rounds=progress.rounds,
                calls=progress.calls,
                seed=progress.seed,
                rolls=progress.rolls,
                transcript=progress.transcript,
                backfill=progress.backfill,
                summary=_clip(closing.get("summary"), 200),
                salience=salience,
                event_id=event_id,
                backfill_event_id=backfill_event_id,
            )
            canon_added = await self.store.add_canon(
                bot_id, world.clean_canon(closing.get("canon")), scene_id, ended
            )
            note_counts = await self.store.apply_notes(
                bot_id, world.clean_notes(closing.get("notes")), scene_id, ended
            )
            await self.store.update_state(
                bot_id,
                ended,
                card=card.to_json(),
                tokens=tokens,
                tokens_at=ended,
                next_wake_at=next_wake_at,
                last_scene_at=ended,
                failures=0,
            )

        # 建边和“要不要进常驻背景”失败都不该让已经发生的事作废，所以放在事务之后。
        if memory["entities"] and cfg.bool("edges.enabled", True):
            try:
                await self.memory.edges.link(
                    bot_id,
                    event_id,
                    memory["entities"],
                    ended,
                    max_degree=cfg.int("edges.max_degree", 20, lo=1, hi=200),
                )
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("故事事件 %s 建边失败：%s", event_id, exc)
        try:
            await self.memory.ambient.on_event_closed(
                {
                    "event_id": event_id,
                    "bot_id": bot_id,
                    "turns": [],
                    "fragment": memory["fragment"],
                    "summary": memory["summary"],
                    "salience": salience,
                    "now": ended,
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.audit.warn("故事事件 %s 常驻背景判断失败：%s", event_id, exc)

        self.audit.event(
            "story_scene",
            bot_id=bot_id,
            scene_id=scene_id,
            event_id=event_id,
            cause=progress.cause,
            rounds=progress.rounds,
            calls=progress.calls,
            rolls=len(progress.rolls),
            salience=salience,
            backfill=bool(backfill_event_id),
            canon_added=canon_added,
            notes=note_counts,
            wake_minutes=wake_minutes,
            tokens=round(tokens, 2),
        )
        return SceneResult(
            scene_id=scene_id,
            event_id=event_id,
            backfill_event_id=backfill_event_id,
            rounds=progress.rounds,
            calls=progress.calls,
            salience=salience,
            fragment=memory["fragment"],
            next_wake_at=next_wake_at,
            tokens=tokens,
            card=card,
        )
