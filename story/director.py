"""一次唤醒的编排：开场（含补叙）→ 多轮往返与裁定 → 收场 → 进入记忆。

背景模型是主持人：叙述她能感知的情境、裁定结果。她是玩家：只决定自己的想法和行动。
一场要么完整落库（记忆事件、状态卡、设定集、笔记、下次预约、扣额度），要么整场丢弃。
"""

from __future__ import annotations

import inspect
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
from . import bucket, cast, link, oracle, prompts, tree, world
from .card import MINUTE_MS, StatusCard, build_card, load_card, render_for_gm
from .llm import ROLE_ACTOR, ROLE_GM, StoryLLM, StoryLLMError
from .recall import SCENE_REF, PastScenes
from .store import StoryStore, story_umo

MAX_LOOKUPS = 6

ConfigProvider = Callable[[str], ConfigView]
# bot_id -> (基础人设文本, 来源说明)
BaseCardResolver = Callable[[str], Awaitable[tuple[str, str]]]

NARRATOR_NAME = "旁白"


def _remote_outcomes(raw: Any) -> dict[int, str]:
    if not isinstance(raw, list):
        return {}
    found: dict[int, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            remote_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        status = link.OUTCOMES.get(str(item.get("outcome") or "").strip())
        if status:
            found[remote_id] = status
    return found


def _match_session(
    rows: list[dict[str, Any]], target: str
) -> tuple[str, str, str] | list[str] | None:
    """唯一对上时给出 (umo, 显示名, 类型)；重名给出会话号列表；没有就是 None。"""
    exact = [row for row in rows if str(row.get("umo") or "") == target]
    if len(exact) == 1:
        row = exact[0]
        return str(row["umo"]), str(row.get("name") or target), str(row.get("type") or "")
    named = [row for row in rows if str(row.get("name") or "") == target]
    if len(named) == 1:
        row = named[0]
        return str(row["umo"]), str(row.get("name") or target), str(row.get("type") or "")
    if len(named) > 1:
        return [str(row.get("umo")) for row in named]
    return None


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
    settled: set[int] = field(default_factory=set)
    settled_umos: set[str] = field(default_factory=set)
    address_retry: str = ""  # "" 还没用 / open 下一轮重发 / used 已经重发过
    nominations: list[dict[str, Any]] = field(default_factory=list)
    retirements: list[str] = field(default_factory=list)
    present: list[str] | None = None
    cast_rows: list[dict[str, Any]] = field(default_factory=list)
    seen_npcs: list[str] = field(default_factory=list)
    note_writes: int = 0


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


THINKING_STRATEGIES = {"off", "close", "scene", "plan"}


def _thinking_strategy(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in THINKING_STRATEGIES else "close"


def _yes(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes", "是"}


def _valid_think(value: dict[str, Any]) -> bool:
    return "think" in value


def _valid_plan(value: dict[str, Any]) -> bool:
    return any(
        str(value.get(key) or "").strip()
        for key in ("established", "unresolved", "consequences")
    )


def _valid_closing(value: dict[str, Any]) -> bool:
    card = value.get("card")
    return isinstance(card, dict) and bool(
        str(card.get("where") or "").strip() or str(card.get("doing") or "").strip()
    )


REAL_PERSON_FEEDBACK = (
    "叙述里出现了「{name}」。这是她手机那头现实里的人，不属于这个世界："
    "不能出现、说话、来电或发消息。请改写，叙述里不要再提到这个名字。"
)

REAL_SPEECH_FEEDBACK = (
    "叙述里替「{name}」写了原话。这个名字可以出现，因为他有一条还没了结的远方动作；"
    "但他没说过的话不能写。请改写，不要用引号写他说了什么。"
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
    base: Callable[[dict[str, Any]], bool],
    names: list[str],
    style: Any = None,
    allowed: set[str] | None = None,
):
    """在结构校验之外，再拦住“替现实里的人说话”，以及第一人称下“替她说话”。

    背景模型能从聊天摘要里看到她和谁聊过。要是它在场景里写“某某发来消息”，她就会
    记住一段现实里从没发生过的对话。提示词里禁止过，但模型不一定照做，所以这里硬拦。
    未了结的远方动作上的名字可以出现，但引号里的原话仍然整段作废。
    """
    first_person = prompts.narration(style).key == "first"
    opened = allowed or set()

    def validator(value: dict[str, Any]) -> bool | str:
        if not base(value):
            return False
        text = _narrated(value)
        name = _mentioned(names, text)
        if name and name not in opened:
            return REAL_PERSON_FEEDBACK.format(name=name)
        if name and name in opened and link.quoted_by(name, text):
            return REAL_SPEECH_FEEDBACK.format(name=name)
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
        send_text: Callable[[str, str], Awaitable[bool]] | None = None,
        search_tools: Callable[..., Any] | None = None,
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
        self.send_text = send_text
        self._search_tools = search_tools
        self.past = PastScenes(store, getattr(memory, "jev", None), audit)

    def _search(self, bot_id: str = "") -> Any:
        """AstrBot 已启用的网页搜索。没开、或这一刻装不上，就当没有工具。"""
        if self._search_tools is None:
            return None
        try:
            params = inspect.signature(self._search_tools).parameters.values()
            accepts_arg = any(
                param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD, param.VAR_POSITIONAL)
                for param in params
            )
            return self._search_tools(bot_id) if accepts_arg else self._search_tools()
        except Exception as exc:  # noqa: BLE001 - 搜索装不上不该让这场演不下去
            self.audit.warn("故事网页搜索不可用：%s", exc)
            return None

    @staticmethod
    def _take_present(value: dict[str, Any], progress: SceneProgress) -> None:
        if "present" not in value:
            return
        progress.present = cast.clean_present(value.get("present"))

    async def _resolve_cast(
        self, bot_id: str, progress: SceneProgress, cap: int, tools: cast.CastTools
    ) -> None:
        """在场名单里，活跃的直接上场；休眠的按空位当场恢复。其余留给背景模型当龙套。"""
        if progress.present is None:
            progress.cast_rows = []
            return
        # 用库里的现状。这一场登记的休眠还没提交，不算空位。
        rows = await self.store.npcs(bot_id)
        by_name = {str(row["name"]): row for row in rows}
        free = max(0, cap - sum(1 for row in rows if row.get("status") == "active"))
        chosen: list[dict[str, Any]] = []
        for name in progress.present:
            row = by_name.get(name)
            if row is not None and row.get("status") == "active":
                chosen.append(row)
                continue
            if row is not None and row.get("status") == "dormant" and free > 0:
                await self.store.set_npc_status(str(row["npc_bot_id"]), "active", self.now_fn())
                try:
                    await self.memory.decay(str(row["npc_bot_id"]))
                except Exception as exc:  # noqa: BLE001
                    self.audit.warn("配角 %s 恢复时衰减失败：%s", name, exc)
                row = dict(row)
                row["status"] = "active"
                by_name[name] = row
                free -= 1
                chosen.append(row)
        progress.cast_rows = chosen
        tools.roster = await self.store.npcs(bot_id)

    async def _play_cast(
        self, bot_id: str, progress: SceneProgress, style: Any, started: int
    ) -> None:
        for row in list(progress.cast_rows):
            npc_id = str(row["npc_bot_id"])
            try:
                prepared = await self.memory.prepare_turn(
                    npc_id, story_umo(npc_id), progress.transcript[-1]["text"], [], started
                )
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("配角 %s 的记忆没准备好：%s", row.get("name"), exc)
                prepared = None
            try:
                action, used = await self.llm.ask_json(
                    "npc",
                    bot_id,
                    prompts.npc_system(
                        str(row["name"]),
                        persona=getattr(prepared, "persona_text", "") or "",
                        ambient=getattr(prepared, "ambient_text", "") or "",
                    ),
                    prompts.npc_user(progress.transcript, style),
                    validate=_valid_action,
                )
            except StoryLLMError as exc:
                self.audit.warn("配角 %s 这一下没写成：%s", row.get("name"), exc)
                continue
            progress.calls += used
            if prepared is not None and prepared.injection is not None and prepared.injection.candidates > 0:
                progress.calls += 1
            text = _prose(action.get("action"), 240)
            progress.transcript.append(
                {
                    "role": "npc",
                    "name": str(row["name"]),
                    "inner": _prose(action.get("inner"), 120),
                    "text": text,
                }
            )
            if npc_id not in progress.seen_npcs:
                progress.seen_npcs.append(npc_id)

    async def _finish_nominations(
        self, bot_id: str, progress: SceneProgress, now: int, cap: int
    ) -> None:
        """场景已经提交。先算上刚休眠的人，再按登记顺序占空位。"""
        rows = await self.store.npcs(bot_id)
        active = sum(1 for row in rows if row.get("status") == "active")
        for item in progress.nominations:
            if active >= cap:
                self.audit.warn("配角「%s」没有空位，这场不建号", item.get("name"))
                continue
            seq = await self.store.next_npc_seq(bot_id)
            npc_id = cast.npc_bot_id(bot_id, seq)
            try:
                await self.memory.import_seed(npc_id, item["seed"])
                await self.store.insert_npc(
                    bot_id, npc_id, str(item["name"]), str(item["relationship"]), now
                )
            except Exception as exc:  # noqa: BLE001
                self.audit.warn("配角「%s」没能建号：%s", item.get("name"), exc)
                continue
            active += 1

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
        min_wake = cfg.int("story.min_wake_minutes", 0, lo=0, hi=10080)
        max_wake = cfg.int("story.max_wake_minutes", 0, lo=0, hi=10080)
        max_rounds = cfg.int("story.max_rounds", 4, lo=1, hi=8)
        style = cfg.str("story.narration", prompts.DEFAULT_NARRATION)
        strategy = _thinking_strategy(cfg.str("story.thinking", "close"))
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
        intents = await self.store.open_intents(bot_id)
        unreads = await self.store.unreads(bot_id)
        remotes = await self.store.open_remotes(bot_id)
        brief = link.render_brief(intents, unreads, remotes)
        allowed = {str(row.get("display_name") or "") for row in remotes} - {""}
        intent_by_id = {int(row["id"]): str(row["umo"]) for row in intents}
        if brief:
            context += "\n\n" + brief
        npc_cap = cfg.int("story.npc_active_max", 3, lo=1, hi=20)
        idle_days = cfg.int("story.npc_idle_days", 14, lo=1, hi=3650)
        await self.store.sleep_idle(bot_id, started, idle_days * 86_400_000)
        roster = await self.store.npcs(bot_id)
        context += "\n\n" + cast.render_roster(roster, npc_cap)
        gm_tools = cast.CastTools(
            progress, roster, cap=npc_cap, web=self._search(bot_id), store=self.store, bot_id=bot_id
        )
        # 默认不拦真名。关掉时不收集名单，名字和引号原话都不再作废；替她开口仍在 _guarded 里。
        if cfg.bool("story.guard_real_names", False):
            real_names = await self._real_names(bot_id, state, canon, context)
        else:
            real_names = []
        think_scene, constraints = await self._prepare_thinking(
            bot_id, strategy, context, progress
        )
        if constraints:
            context += "\n\n# 这场的约束（她看不到）\n" + constraints
        # scene：判断要多想时，这一场的背景模型调用都开思考。
        # close：只有收场开。plan 的思考用在约束上，执行本身不开。
        scene_thinking = strategy == "scene" and think_scene
        close_thinking = scene_thinking or strategy == "close"

        # ---- 开场（含补叙）
        opening, used = await self.llm.ask_json(
            ROLE_GM,
            bot_id,
            gm_system,
            context
            + "\n\n"
            + prompts.gm_open_task(first=first, backfill=want_backfill, style=style),
            validate=_guarded(_valid_opening, real_names, style, allowed),
            thinking=scene_thinking,
            search=gm_tools,
        )
        progress.calls += used
        progress.backfill = _prose(opening.get("backfill"), 300) if want_backfill else ""
        progress.transcript.append({"role": "narrator", "text": _prose(opening["narration"], 400)})
        lookups = await self._lookups(bot_id, canon, opening.get("lookup"), style)
        self._take_present(opening, progress)
        await self._resolve_cast(bot_id, progress, npc_cap, gm_tools)

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
        round_no = 0
        while round_no <= max_rounds:
            round_no += 1
            if round_no > max_rounds:
                if progress.address_retry != "open":
                    break
                progress.address_retry = "used"
            seen = (
                [{"role": "narrator", "text": progress.backfill}] if progress.backfill else []
            ) + progress.transcript
            action, used = await self.llm.ask_json(
                ROLE_ACTOR,
                bot_id,
                actor_system,
                prompts.actor_user(seen, recall_parts, style, brief),
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
            had_messages = bool(link.parse_messages(action.get("messages")))
            address_failed = await self._deliver(bot_id, cfg, action.get("messages"), progress)
            if address_failed and progress.address_retry == "":
                progress.address_retry = "open"
            elif progress.address_retry == "open" and had_messages:
                # 对上号再发一次就算用掉这次机会，后面不再因为对不上人加轮。
                progress.address_retry = "used"
            lookups = lookups + self._mentioned_canon(
                canon, progress.transcript[-1]["text"], context, lookups
            )
            await self._play_cast(bot_id, progress, style, started)

            last = round_no > max_rounds or (
                round_no >= max_rounds and progress.address_retry != "open"
            )
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
                validate=_guarded(_valid_turn, real_names, style, allowed),
                thinking=scene_thinking,
                search=gm_tools,
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
            self._take_settled(turn.get("settled"), intent_by_id, progress)
            self._take_present(turn, progress)
            await self._resolve_cast(bot_id, progress, npc_cap, gm_tools)
            stop = last or bool(turn.get("end"))
            if stop and progress.address_retry == "open":
                continue
            if stop:
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
            validate=_guarded(_valid_closing, real_names, style, allowed),
            thinking=close_thinking,
            search=gm_tools,
            tools_then_think=close_thinking,
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

    async def _prepare_thinking(
        self, bot_id: str, strategy: str, context: str, progress: SceneProgress
    ) -> tuple[bool, str]:
        """scene / plan 先判断要不要多想。plan 再写一场的约束，只放进这次的上下文，不落库。"""
        if strategy not in {"scene", "plan"}:
            return False, ""
        try:
            decision, used = await self.llm.ask_json(
                ROLE_GM,
                bot_id,
                prompts.THINK_DETECTOR_SYSTEM,
                context,
                validate=_valid_think,
            )
        except StoryLLMError as exc:
            progress.calls += exc.attempts
            return False, ""
        progress.calls += used
        if not _yes(decision.get("think")):
            return False, ""
        if strategy != "plan":
            return True, ""
        try:
            plan, used = await self.llm.ask_json(
                ROLE_GM,
                bot_id,
                prompts.THINK_PLANNER_SYSTEM,
                context,
                validate=_valid_plan,
                thinking=True,
            )
        except StoryLLMError as exc:
            progress.calls += exc.attempts
            return False, ""
        progress.calls += used
        return True, prompts.render_constraints(plan)

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
        """背景模型点名要查的东西：#编号查那一场的经过，设定名查设定，其余当关键词翻以前的场景。"""
        if not isinstance(names, list):
            return []
        wanted = [str(name or "").strip() for name in names if str(name or "").strip()]
        found: list[str] = []
        words: list[str] = []
        for name in wanted[:MAX_LOOKUPS]:
            ref = SCENE_REF.match(name)
            if ref:
                text = await self.past.scene_text(bot_id, int(ref.group(1)), style)
                found.append(text or f"（没有 {name} 这一场）")
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

        card_text = render_for_gm(card, now)
        progress.past = []
        stored = tree.loads(state.get("tree"))
        linked = {
            name
            for thread in stored.get("threads") or []
            for name in list(thread.get("canon") or []) + list(thread.get("seed") or [])
        }
        index = [str(entry["name"]) for entry in canon if str(entry.get("name") or "") not in linked][:80]
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
            canon_index=index,
            tree=tree.render(stored, []),
            card=card_text,
            digest=digest,
            seed=seed,
            last_ending=last_ending,
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

    async def _next_wake(
        self,
        bot_id: str,
        closing: dict[str, Any],
        progress: SceneProgress,
        ended: int,
        min_wake: int,
        max_wake: int,
    ) -> tuple[int | None, int | None]:
        """分钟数，以及下次唤醒的时刻。没约上时两个都是 None。"""
        kind, minutes = link.wake_request(closing.get("next_wake_minutes"))
        if kind == "ask":
            try:
                follow, used = await self.llm.ask_json(
                    ROLE_GM,
                    bot_id,
                    "你只补这一场忘了写的下次时间。不要改已经收好的场。",
                    "只输出 JSON：{\"next_wake_minutes\": 60}。0 表示这场一结束就可以继续。"
                    "不小于 0 的整数。如果你不打算再自动醒，就不要给这个字段。",
                    validate=lambda value: "next_wake_minutes" in value
                    if isinstance(value, dict)
                    else False,
                )
                progress.calls += used
            except StoryLLMError as exc:
                progress.calls += exc.attempts
                return None, None
            kind, minutes = link.wake_request(follow.get("next_wake_minutes"))
            if kind == "ask":
                return None, None
        minutes = link.clamp_wake(minutes, min_wake=min_wake, max_wake=max_wake)
        return minutes, ended + minutes * MINUTE_MS

    @staticmethod
    def _take_settled(raw: Any, intent_by_id: dict[int, str], progress: SceneProgress) -> None:
        if not isinstance(raw, list):
            return
        for item in raw:
            try:
                intent_id = int(item)
            except (TypeError, ValueError):
                continue
            umo = intent_by_id.get(intent_id)
            if umo:
                progress.settled.add(intent_id)
                progress.settled_umos.add(umo)

    async def _deliver(
        self,
        bot_id: str,
        cfg: ConfigView,
        raw: Any,
        progress: SceneProgress,
    ) -> bool:
        """发出她这一轮要说的话。返回值表示有没有因为对不上人而需要再给一次。"""
        messages = link.parse_messages(raw)
        if not messages:
            return False
        per_round = cfg.int("story.share_per_round", 0, lo=0, hi=20)
        address_failed = False
        for index, (target, text) in enumerate(messages):
            if per_round and index >= per_round:
                self._notice(progress, "这一轮我说得太多了，剩下的先不发。")
                break
            outcome = await self._deliver_one(bot_id, cfg, target, text, progress)
            if outcome == "address" and progress.address_retry != "used":
                address_failed = True
        return address_failed

    async def _deliver_one(
        self,
        bot_id: str,
        cfg: ConfigView,
        target: str,
        text: str,
        progress: SceneProgress,
    ) -> str:
        now = self.now_fn()
        days = cfg.int("memo.catalog_days", 7, lo=1, hi=365)
        limit = cfg.int("memo.catalog_limit", 10, lo=1, hi=100)
        rows = await self.store.catalog(bot_id, now - days * 86_400_000, limit)
        match = _match_session(rows, target)
        again = "" if progress.address_retry == "used" else "这场还能再发一次。"
        if match is None:
            self._notice(progress, f"我没把这句话发给「{target}」。没有这个人。")
            return "address"
        if isinstance(match, list):
            shown = "、".join(match)
            self._notice(
                progress,
                f"我没把这句话发出去，「{target}」对上了好几个人。会话号：{shown}。{again}",
            )
            return "address"
        umo, name, kind = match
        unread = {str(row["umo"]) for row in await self.store.unreads(bot_id)}
        outbound = link.classify_outbound(
            umo, settled=progress.settled_umos, unread=unread
        )
        if str(kind) == "group" and outbound == "share":
            if name not in link.group_names(cfg.str("story.share_groups", "")):
                self._notice(progress, f"我没把这句话发到群「{name}」里。这个群不在可以主动说的名单上。")
                return "blocked"
        if link.quiet_active(cfg.str("story.quiet_hours", ""), now):
            self._notice(progress, "这会儿太晚了，我没把这句话发出去。")
            return "blocked"
        cooldown = cfg.int("story.share_cooldown_minutes", 0, lo=0, hi=1440)
        if cooldown:
            last = await self.store.last_send_at(bot_id, umo)
            if last and now - last < cooldown * MINUTE_MS:
                self._notice(progress, f"我刚跟{name}说过，这会儿先不再发。")
                return "blocked"
        if outbound == "share":
            ok = await self._take_share(bot_id, cfg, now)
            if not ok:
                self._notice(progress, "我今天说得够多了，这条没发出去。")
                return "blocked"
        if self.send_text is None:
            self._notice(progress, "我这会儿发不出去。")
            return "blocked"
        sent = await self.send_text(umo, text)
        if not sent:
            if outbound == "share":
                await self._refund_share(bot_id, now)
            self._notice(progress, f"我跟{name}说的那句没送出去。")
            return "blocked"
        await self.store.add_send(bot_id, umo, outbound, text, now)
        await self._remember_chat(bot_id, umo, text, now)
        if outbound == "catchup":
            await self.store.drop_unread(bot_id, umo)
        self._notice(progress, f"我跟{name}说了：{text}")
        return "sent"

    async def _take_share(self, bot_id: str, cfg: ConfigView, now: int) -> bool:
        state = await self.store.state(bot_id) or {}
        per_day = cfg.float("story.share_per_day", 20, lo=0, hi=1000)
        capacity = cfg.float("story.share_capacity", 20, lo=0, hi=1000)
        at = int(state.get("share_tokens_at") or 0)
        current = float(state.get("share_tokens") or 0.0)
        if not at:
            current = bucket.initial(per_day=per_day, capacity=capacity)
            at = now
        tokens = bucket.refill(current, at, now, per_day=per_day, capacity=capacity)
        if tokens < 1:
            await self.store.update_state(bot_id, now, share_tokens=tokens, share_tokens_at=now)
            return False
        await self.store.update_state(
            bot_id, now, share_tokens=tokens - 1, share_tokens_at=now
        )
        return True

    async def _refund_share(self, bot_id: str, now: int) -> None:
        state = await self.store.state(bot_id) or {}
        tokens = float(state.get("share_tokens") or 0.0) + 1
        await self.store.update_state(bot_id, now, share_tokens=tokens, share_tokens_at=now)

    async def _remember_chat(self, bot_id: str, umo: str, text: str, now: int) -> None:
        event_id = await self.db.execute(
            "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, participants, "
            "turn_count, char_count, fragment, summary, entities, storage_strength, "
            "retrievability, last_recalled_at, salience, status, source, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                bot_id,
                umo,
                "episodic",
                now,
                now,
                "[]",
                1,
                len(text),
                text[:200],
                None,
                "[]",
                self.memory.dynamics.s0_event(2),
                1.0,
                now,
                2,
                "closed",
                "chat",
                now,
            ),
        )
        await self.db.execute(
            "INSERT INTO event_turns(event_id, seq, role, speaker_id, speaker_name, text, ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (event_id, 1, "assistant", "", "", text, now),
        )

    @staticmethod
    def _notice(progress: SceneProgress, text: str) -> None:
        progress.transcript.append({"role": "notice", "text": text})

    async def _store_tree(
        self, bot_id: str, cfg: ConfigView, closing: dict[str, Any], scene_id: int
    ) -> None:
        """收场交来的线。结构不对就留下上一棵树。日常节点由程序补。"""
        if "tree" not in closing:
            return
        canon = await self.store.canon(bot_id)
        npcs = await self.store.npcs(bot_id)
        people = {str(row.get("name") or "") for row in npcs}
        people.update(
            str(row.get("name") or "") for row in canon if row.get("category") == "person"
        )
        people.discard("")
        briefs = await self.store.scene_briefs(bot_id)
        known = {int(row["id"]) for row in briefs}
        known.add(int(scene_id))
        cleaned = tree.clean_threads(
            closing.get("tree"),
            people=people,
            canon={str(row.get("name") or "") for row in canon},
            seed={str(row.get("name") or "") for row in canon if row.get("origin") == "seed"},
            scene_ids=known,
            current_scene_id=scene_id,
        )
        if cleaned is None:
            return
        filled = tree.fill_daily(
            cleaned,
            briefs,
            cfg.int("story.recent_scenes", 3, lo=0, hi=20),
        )
        await self.store.update_state(bot_id, tree=json.dumps(filled, ensure_ascii=False))

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
        card_cap = max_wake if max_wake > 0 else 10080
        card = build_card(closing.get("card"), ended, max_minutes=card_cap)
        assert card is not None  # _valid_closing 已保证
        wake_minutes, next_wake_at = await self._next_wake(
            bot_id, closing, progress, ended, min_wake, max_wake
        )

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
            if turn.get("role") == "npc":
                who = str(turn.get("name") or "配角").strip()
                turns.append(("narrator", f"{who}：{text}" if text else who))
            elif turn.get("role") in {"narrator", "notice"}:
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
            note_counts = {"written": progress.note_writes, "deleted": 0}
            await self._store_tree(bot_id, cfg, closing, scene_id)
            await self.store.settle_intents(bot_id, progress.settled, scene_id, ended)
            await self.store.resolve_remotes(
                bot_id, _remote_outcomes(closing.get("remote")), scene_id, ended
            )
            await self.store.retire_named(bot_id, progress.retirements, ended)
            await self.store.touch_npcs(progress.seen_npcs, ended)
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

        await self._finish_nominations(
            bot_id, progress, ended, cfg.int("story.npc_active_max", 3, lo=1, hi=20)
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
            scheduled=next_wake_at is not None,
        )
        return SceneResult(
            scene_id=scene_id,
            event_id=event_id,
            backfill_event_id=backfill_event_id,
            rounds=progress.rounds,
            calls=progress.calls,
            salience=salience,
            fragment=memory["fragment"],
            next_wake_at=int(next_wake_at or 0),
            tokens=tokens,
            card=card,
        )
