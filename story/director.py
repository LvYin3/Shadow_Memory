"""一次唤醒的编排：开场（含补叙）→ 多轮往返与裁定 → 收场 → 进入记忆。

背景模型是主持人：叙述她能感知的情境、裁定结果。她是玩家：只决定自己的想法和行动。
一场要么完整落库（记忆事件、状态卡、设定集、笔记、下次预约、扣额度），要么整场丢弃。
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..core import clock as clock_mod
from ..core.audit import Audit
from ..core.config import ConfigView
from ..core.db import Database
from ..memory import prompts as memory_prompts
from ..memory.edges import normalize_entities
from ..memory.service import MemoryService
from ..memory.textutil import trim_at_boundary
from . import bucket, oracle, prompts, world
from .card import MINUTE_MS, StatusCard, build_card, load_card, render_for_gm
from .llm import ROLE_ACTOR, ROLE_GM, StoryLLM, StoryLLMError
from .recall import SCENE_REF, PastScenes, scene_line
from .store import StoryStore, story_umo

MAX_LOOKUPS = 6

ConfigProvider = Callable[[str], ConfigView]
# bot_id -> (基础人设文本, 来源说明)
BaseCardResolver = Callable[[str], Awaitable[tuple[str, str]]]

NARRATOR_NAME = "旁白"


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _prose(value: Any, limit: int) -> str:
    """叙述类文本超长时尽量断在句末，不把一句话拦腰截断。"""
    return trim_at_boundary(str(value or ""), limit)


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
    past: list[str] = field(default_factory=list)  # 开场带上的更早场景


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


REAL_PERSON_FEEDBACK = (
    "叙述里出现了「{name}」。这是她手机那头现实里的人，不属于这个世界："
    "不能出现、说话、来电或发消息。请改写，叙述里不要再提到这个名字。"
)


SPOKE_FOR_HER_FEEDBACK = (
    "叙述里替她说了话（「{quote}」）。她说什么由扮演她的人自己写；"
    "请改写，只写她看到、听到、感觉到的，不要出现她开口说的话。"
)

# 第一人称叙述里“我说：“……””这样的句子就是替她开口；只在第一人称下拦。
_SPOKE_FOR_HER = re.compile(r"我(?:说|问|答|喊|回答|应)道?[：:，,\s]*[“「\"]")


def _spoke_for_her(text: str) -> str:
    match = _SPOKE_FOR_HER.search(text)
    return text[match.start() : match.end() + 12] if match else ""


def _narrated(value: dict[str, Any]) -> str:
    """背景模型这次输出里会被她读到的全部文字。"""
    parts = [value.get("backfill"), value.get("narration"), value.get("epilogue")]
    check = value.get("check")
    if isinstance(check, dict):
        parts += [check.get("what"), check.get("success"), check.get("failure")]
    return "\n".join(str(part) for part in parts if part)


def _mentioned(names: list[str], text: str) -> str:
    return next((name for name in names if name in text), "")


def _guarded(
    base: Callable[[dict[str, Any]], bool], names: list[str], style: Any = None
):
    """在结构校验之外，再拦住“替现实里的人说话”，以及第一人称下“替她说话”。

    背景模型能从聊天摘要里看到她和谁聊过。要是它在场景里写“某某发来消息”，她就会
    记住一段现实里从没发生过的对话。提示词里禁止过，但模型不一定照做，所以这里硬拦。
    """
    first_person = prompts.narration(style).key == "first"

    def validator(value: dict[str, Any]) -> bool | str:
        if not base(value):
            return False
        text = _narrated(value)
        name = _mentioned(names, text)
        if name:
            return REAL_PERSON_FEEDBACK.format(name=name)
        quote = _spoke_for_her(text) if first_person else ""
        return SPOKE_FOR_HER_FEEDBACK.format(quote=quote) if quote else True

    return validator


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
        self.past = PastScenes(store, getattr(memory, "jev", None), audit)

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
        style = cfg.str("story.narration", prompts.DEFAULT_NARRATION)
        gm_system = prompts.gm_system(style)

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
            progress=progress,
        )
        real_names = await self._real_names(bot_id, state, canon, context)

        # ---- 开场（含补叙）
        opening, used = await self.llm.ask_json(
            ROLE_GM,
            bot_id,
            gm_system,
            context
            + "\n\n"
            + prompts.gm_open_task(first=first, backfill=want_backfill, style=style),
            validate=_guarded(_valid_opening, real_names, style),
        )
        progress.calls += used
        progress.backfill = _prose(opening.get("backfill"), 300) if want_backfill else ""
        progress.transcript.append({"role": "narrator", "text": _prose(opening["narration"], 400)})
        lookups = await self._lookups(bot_id, canon, opening.get("lookup"), style)

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
            style=style,
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
                prompts.actor_user(seen, recall_parts, style),
                validate=_valid_action,
            )
            progress.calls += used
            progress.transcript.append(
                {
                    "role": "me",
                    "inner": _prose(action.get("inner"), 120),
                    "text": _prose(action["action"], 240),
                }
            )
            progress.rounds = round_no
            lookups = lookups + self._mentioned_canon(
                canon, progress.transcript[-1]["text"], context, lookups
            )

            last = round_no >= max_rounds
            turn, used = await self.llm.ask_json(
                ROLE_GM,
                bot_id,
                gm_system,
                "\n\n".join(
                    [
                        context,
                        prompts.gm_progress(
                            progress.transcript,
                            backfill=progress.backfill,
                            twist=twist,
                            lookups=lookups,
                            style=style,
                        ),
                        prompts.gm_turn_task(last=last),
                    ]
                ),
                validate=_guarded(_valid_turn, real_names, style),
            )
            progress.calls += used
            lookups = await self._lookups(bot_id, canon, turn.get("lookup"), style)
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
            progress.transcript.append({"role": "narrator", "text": _prose(narration, 400)})
            if last or bool(turn.get("end")):
                break

        # ---- 收场
        closing, used = await self.llm.ask_json(
            ROLE_GM,
            bot_id,
            gm_system,
            "\n\n".join(
                [
                    context,
                    prompts.gm_progress(
                        progress.transcript,
                        backfill=progress.backfill,
                        twist=twist,
                        lookups=lookups,
                        style=style,
                    ),
                    prompts.gm_close_task(min_wake=min_wake, max_wake=max_wake, style=style),
                ]
            ),
            validate=_guarded(_valid_closing, real_names, style),
        )
        progress.calls += used
        # 现实里的人也不能被登记成世界设定（笔记是背景模型自己看的，可以提到）。
        if isinstance(closing.get("canon"), list):
            closing["canon"] = [
                item
                for item in closing["canon"]
                if isinstance(item, dict)
                and not _mentioned(real_names, f"{item.get('name')}\n{item.get('body')}")
            ]
        epilogue = _prose(closing.get("epilogue"), 200)
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
    async def _real_names(
        self, bot_id: str, state: dict[str, Any], canon: list[dict[str, Any]], context: str
    ) -> list[str]:
        """背景模型这次能看到的、现实里和她聊过天的人的名字。

        只取出现在它上下文里的（它不知道的名字不会去写），并排除与世界种子撞名的
        （种子是用户写的，撞名时以世界里的人物为准）。单字的名字太容易误伤，不拦。
        """
        rows = await self.db.fetchall(
            "SELECT name, aliases FROM people WHERE bot_id=?", (bot_id,)
        )
        names: set[str] = set()
        for row in rows:
            names.add(str(row.get("name") or "").strip())
            try:
                aliases = json.loads(str(row.get("aliases") or "[]"))
            except (TypeError, ValueError):
                aliases = []
            if isinstance(aliases, list):
                names.update(str(alias).strip() for alias in aliases)
        meta = self.store.seed_meta(state)
        opening = meta.get("opening") if isinstance(meta.get("opening"), dict) else {}
        world_text = "\n".join(
            [
                str(state.get("world_title") or ""),
                str(state.get("outline") or ""),
                *[str(hook) for hook in meta.get("hooks") or []],
                *[str(value) for value in opening.values()],
                *[
                    f"{entry.get('name')} {entry.get('body')}"
                    for entry in canon
                    if entry.get("origin") == "seed"
                ],
            ]
        )
        return sorted(
            (
                name
                for name in names
                if len(name) >= 2
                and name != "对方"
                and name in context
                and name not in world_text
            ),
            key=len,
            reverse=True,
        )

    async def _lookups(
        self, bot_id: str, canon: list[dict[str, Any]], names: Any, style: Any
    ) -> list[str]:
        """背景模型点名要查的东西：#编号 查那一场的经过，笔记标题查笔记，设定名查设定，
        其余当作关键词去翻以前场景的梗概。"""
        if not isinstance(names, list):
            return []
        wanted = [str(name or "").strip() for name in names if str(name or "").strip()]
        found: list[str] = []
        words: list[str] = []
        notes: list[dict[str, Any]] | None = None
        for name in wanted[:MAX_LOOKUPS]:
            ref = SCENE_REF.match(name)
            if ref:
                text = await self.past.scene_text(bot_id, int(ref.group(1)), style)
                found.append(text or f"（没有 {name} 这一场）")
                continue
            if notes is None:
                notes = await self.store.notes(bot_id)
            note = next((note for note in notes if note["title"] == name), None)
            if note is not None:
                found.append(f"- [笔记] {note['title']}：{note['body']}")
                continue
            entries = world.find_by_names(canon, [name], 2)
            if entries:
                found.extend(world.render_entry(entry) for entry in entries)
                continue
            words.append(name)
        missed: list[str] = []
        for word in words:
            scenes = await self.past.search(bot_id, [word], 2)
            found.extend(f"- [以前的场景] {line}" for line in scenes)
            if not scenes:
                missed.append(word)
        if missed:
            found.append(f"（没有查到：{'、'.join(missed)}）")
        unique: list[str] = []
        for line in found:
            if line not in unique:
                unique.append(line)
        return unique

    @staticmethod
    def _mentioned_canon(
        canon: list[dict[str, Any]], action: str, context: str, shown: list[str]
    ) -> list[str]:
        """她这一步点到了某个设定、而背景模型手边没有它的内容时，替它查出来。"""
        lines: list[str] = []
        for entry in canon:
            name = str(entry.get("name") or "")
            if len(name) < 2 or name not in action:
                continue
            line = world.render_entry(entry)
            if line not in context and line not in shown and line not in lines:
                lines.append(line)
        return lines[:MAX_LOOKUPS]

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
        progress: SceneProgress,
    ) -> str:
        meta = self.store.seed_meta(state)
        opening = meta.get("opening") if isinstance(meta.get("opening"), dict) else {}
        card = load_card(state.get("card"))

        recent_count = cfg.int("story.recent_scenes", 3, lo=0, hi=20)
        scenes = await self.store.recent_scenes(bot_id, recent_count)
        scene_lines = [
            scene_line(row) for row in reversed(scenes) if str(row.get("summary") or "").strip()
        ]

        # 上一场的结尾（她最后一步和之后的旁白），让新的一场接得上、不重演。
        # 用 gm 视角渲染：只有她外显的言行，不带内心。
        previous = await self.store.last_transcript(bot_id)
        tail_start = max(
            (index for index, turn in enumerate(previous) if turn.get("role") == "me"),
            default=max(0, len(previous) - 1),
        )
        last_ending = prompts.render_transcript(
            previous[tail_start:], viewer="gm", style=cfg.str("story.narration", "")
        )[:600]

        # 只读外显：聊天事件的碎片与摘要。不读人格三层、洞察和私人笔记。
        last_scene_at = int(state.get("last_scene_at") or 0)
        digest = await self.store.chat_digest(
            bot_id,
            last_scene_at or now - 86_400_000,
            cfg.int("story.chat_digest_items", 5, lo=0, hi=20),
        )

        situation = " ".join(
            [
                card.where if card else "",
                card.doing if card else "",
                str(opening.get("where") or ""),
                str(opening.get("doing") or ""),
                *scene_lines,
                *digest,
            ]
        )
        card_text = render_for_gm(card, now)

        # 更早的场景不衰减：粗筛后让判断模型挑出演下一场时值得想起的。
        # 判断模型只看这里给的外显信息，不看她的人格和记忆。
        # 粗筛只看此刻的处境；混进最近几场的梗概，字面上就总是那几场最像。
        pick = await self.past.pick(
            bot_id,
            skip=recent_count,
            query=" ".join(
                [
                    card.where if card else "",
                    card.doing if card else "",
                    str(opening.get("where") or ""),
                    str(opening.get("doing") or ""),
                    last_ending,
                ]
            ),
            state={
                "now": moment,
                "her_status": card_text,
                "recent_scenes": scene_lines,
                "last_ending": last_ending[:400],
            },
            limit=cfg.int("story.past_scenes", 3, lo=0, hi=10),
            candidates=cfg.int("story.past_candidates", 30, lo=1, hi=60),
            threshold=cfg.float("story.past_threshold", 0.6, lo=0.0, hi=1.0),
        )
        if pick.judged:
            progress.calls += 1
        progress.past = list(pick.lines)
        if pick.candidates:
            self.audit.debug(
                "story_past",
                bot_id=bot_id,
                reason=pick.reason,
                candidates=pick.candidates,
                picked=len(pick.lines),
                scores={str(key): round(value, 2) for key, value in pick.scores.items()},
            )

        # 先查再编：按她此刻的处境预取相关设定，其余只给名字。
        query = " ".join([situation, *pick.lines])
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
            card=card_text,
            digest=digest,
            seed=seed,
            last_ending=last_ending,
            past=pick.lines,
        )

    # ------------------------------------------------------------------ 进入记忆
    async def _remember(
        self, bot_id: str, cfg: ConfigView, progress: SceneProgress
    ) -> dict[str, Any]:
        """把场景整理成她的第一人称记忆，并评显著度。整理失败有兜底，不让整场作废。"""
        transcript = prompts.render_transcript(
            progress.transcript, viewer="memory", style=cfg.str("story.narration", "")
        )
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
            summary = _prose(result.get("summary"), 300)
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
                summary=_prose(closing.get("summary"), 200),
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
