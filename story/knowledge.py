"""角色视角的「世界常识」：自动提炼底稿与防剧透分类、常驻渲染、按需 JEV 检索。

设计边界：
- 单一数据源仍在 ``story_state`` 与 ``story_canon``；
- 只有 ``visibility == 'known'`` 的设定与 ``known_outline`` 可以流向主角（聊天主对话 / 故事 Actor）；
- 主角本人不知情的底层秘密、GM 导演元规则、未发生钩子严格停留在 ``gm_only``，绝不外泄；
- 分类或 JEV 异常时保守降级（未审条目一律保持 ``gm_only``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.audit import Audit
from ..core.config import ConfigView
from ..memory.textutil import trim_at_boundary
from . import world
from .store import StoryStore

ConfigProvider = Callable[[str], ConfigView]

LOCAL_FALLBACK_MIN_SCORE = 0.45
INDEX_MAX_ITEMS = 60
INDEX_MAX_CHARS = 600

CLASSIFY_SYSTEM = """你在为一个角色扮演记忆系统拆分「世界种子」，区分“主角本人应该知道的世界常识”与“仅限主持人（GM）掌握的秘密或元规则”。

请严格遵守防剧透红线：
1. known_outline（主角视角的世界常识底稿，200-500 字，不超过 800 字）：
   - 用第一人称「我」概括主角自己明确知道的世界背景、生活地点、公开或自己知情的身份与日常、已知的能力与代价边界、以及她自己需要遵守的保密/生活规矩。
   - 严禁写入任何主角本人不知情的底层秘密、隐藏血脉、被封印的未知能力、幕后真相。
   - 严禁写入给主持人看的元规则（如“故事以……为主”“不演成……”“严格禁止在演出中揭晓……”“时间与现实同步”等导演口吻）或尚未发生的剧情钩子。
2. entries（逐条判定可见性）：
   - "known"：主角本人明确知道或理应了解的世界常识、地点、组织、物品、概念，以及她自己知道并遵守的行为守则（例如保密口径、能力消耗代价）。
   - "gm_only"：主角本人不知情的秘密档案（如标注“本人不知情”“底层秘密”“严禁揭晓”）、主持人/演出元规则、红线禁区、幕后伏笔。
   - 拿不准是否剧透时，一律保守标为 "gm_only"。

只输出合法 JSON 对象，不要解释，不要 markdown 围栏。"""

CLASSIFY_USER = """# 世界标题
{title}

# 世界大纲（可能混有主角已知信息与主持人视角信息）
{outline}

# 设定条目（共 {count} 条）
{entries}

请输出 JSON：
{{"known_outline": "...", "entries": [{{"name": "条目名", "visibility": "known 或 gm_only"}}]}}"""

CLASSIFY_SCHEMA = (
    '{"known_outline": "第一人称世界常识底稿", '
    '"entries": [{"name": "条目名", "visibility": "known"}]}'
)

WORLD_RECALL_INSTRUCTION = (
    "你正在帮主角判断是否需要想起她本来就知道的一条世界常识，以准确回应当前对话或情境。\n"
    "候选设定：[{category}] {name}：{body}\n"
    "结合当前消息和最近上下文，判断：现在回应时想起这条世界常识是否直接有帮助？"
    "（如果对方提到了相关概念、同义/隐晦指代、或正在聊与之直接相关的话题，算有用；"
    "普通寒暄或无关话题算没用。）"
)


@dataclass(slots=True)
class ClassifyResult:
    known_outline: str = ""
    visibilities: dict[str, str] = field(default_factory=dict)
    known_count: int = 0
    gm_only_count: int = 0
    auto_classified: bool = False


@dataclass(slots=True)
class WorldRecallResult:
    entries: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    candidates: int = 0
    scores: dict[str, float] = field(default_factory=dict)
    judged: bool = False


def render_world_brief(
    known_outline: str,
    known_entries: list[dict[str, Any]],
    *,
    include_tool_hint: bool = True,
) -> str:
    """生成可静态缓存的 system prompt 世界常识底稿块。"""
    brief = str(known_outline or "").strip()
    names: list[str] = []
    seen: set[str] = set()
    for entry in known_entries:
        name = str(entry.get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
            if len(names) >= INDEX_MAX_ITEMS:
                break
    if not brief and not names:
        return ""

    lines = [
        "# 我知道的这个世界",
        "- 这些是我本来就了解的世界常识和生活背景；要不要对眼前的人提起、怎么说，按我自己的性格和保密分寸来。",
    ]
    if brief:
        lines.append(brief)
    if names:
        index_text = trim_at_boundary("、".join(names), INDEX_MAX_CHARS)
        suffix = "（需要确认细节时可以用 memory_search 查）" if include_tool_hint else ""
        lines.append(f"- 我熟悉的事物与概念：{index_text}{suffix}")
    return "\n".join(lines)


def format_world_turn_block(entries: list[dict[str, Any]]) -> str:
    """把当轮命中的角色可见设定格式化为临时注入块（保留正文，不做 40 字截断）。"""
    cleaned = [
        entry
        for entry in entries
        if str(entry.get("name") or "").strip() and str(entry.get("body") or "").strip()
    ]
    if not cleaned:
        return ""
    lines = ["【我知道的世界常识，不是对方刚说的；要不要说出口按我自己的分寸】"]
    for entry in cleaned:
        lines.append(world.render_entry(entry))
    return "\n".join(lines)


class WorldKnowledge:
    """管理角色视角的世界底稿提炼、条目可见性分类与按需检索。"""

    def __init__(
        self,
        store: StoryStore,
        configs: ConfigProvider,
        audit: Audit,
        *,
        textgen: Any = None,
        jev: Any = None,
    ) -> None:
        self.store = store
        self.configs = configs
        self.audit = audit
        self.textgen = textgen
        self.jev = jev

    # ------------------------------------------------------------------ 提炼与分类
    async def classify_seed(
        self,
        bot_id: str,
        *,
        title: str,
        outline: str,
        entries: list[dict[str, Any]],
        explicit_known_outline: str = "",
        explicit_visibilities: dict[str, str] | None = None,
    ) -> ClassifyResult:
        """按需调用写作模型提炼角色常识底稿与条目可见性；显式字段严格优先，失败保守回退 gm_only。"""
        explicit_map = {
            str(name): vis
            for name, vis in (explicit_visibilities or {}).items()
            if vis in {"known", "gm_only"}
        }
        explicit_brief = str(explicit_known_outline or "").strip()[: world.KNOWN_OUTLINE_MAX]
        entry_names = [str(item.get("name") or "").strip() for item in entries if str(item.get("name") or "").strip()]
        need_brief = not explicit_brief and bool(str(outline or "").strip())
        missing_names = [name for name in entry_names if name not in explicit_map]

        if not need_brief and not missing_names:
            return self._finalize_classify(explicit_brief, explicit_map, entry_names, auto=False)

        if self.textgen is None:
            return self._finalize_classify(explicit_brief, explicit_map, entry_names, auto=False)

        rendered_entries = "\n".join(
            world.render_entry(item) for item in entries if str(item.get("name") or "").strip()
        )
        raw = None
        try:
            raw = await self.textgen.generate_json(
                CLASSIFY_SYSTEM,
                CLASSIFY_USER.format(
                    title=str(title or "（未命名）").strip(),
                    outline=str(outline or "").strip(),
                    count=len(entry_names),
                    entries=rendered_entries or "（无）",
                ),
                bot_id=bot_id,
                schema_hint=CLASSIFY_SCHEMA,
                task="world_classify",
            )
        except Exception as exc:  # noqa: BLE001 - 分类失败必须保守降级，不能阻断导入
            self.audit.warn("世界种子自动提炼与分类失败（bot=%s）：%s", bot_id, exc)
            return self._finalize_classify(explicit_brief, explicit_map, entry_names, auto=False)

        if not isinstance(raw, dict):
            self.audit.warn("世界种子自动分类返回非 JSON 对象，保持保守默认（bot=%s）", bot_id)
            return self._finalize_classify(explicit_brief, explicit_map, entry_names, auto=False)

        final_brief = explicit_brief
        if need_brief:
            candidate_brief = str(raw.get("known_outline") or "").strip()
            if candidate_brief:
                final_brief = trim_at_boundary(candidate_brief, world.KNOWN_OUTLINE_MAX)

        inferred: dict[str, str] = {}
        raw_items = raw.get("entries")
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                vis = world.normalize_visibility(item.get("visibility"), default="gm_only") or "gm_only"
                if name:
                    inferred[name] = vis
        elif isinstance(raw_items, dict):
            for name, value in raw_items.items():
                vis = world.normalize_visibility(value, default="gm_only") or "gm_only"
                inferred[str(name).strip()] = vis

        merged: dict[str, str] = {}
        for name in entry_names:
            if name in explicit_map:
                merged[name] = explicit_map[name]
            else:
                merged[name] = inferred.get(name, "gm_only")

        return self._finalize_classify(final_brief, merged, entry_names, auto=True)

    @staticmethod
    def _finalize_classify(
        brief: str,
        vis_map: dict[str, str],
        entry_names: list[str],
        *,
        auto: bool,
    ) -> ClassifyResult:
        final_map = {
            name: (vis_map.get(name) if vis_map.get(name) in {"known", "gm_only"} else "gm_only")
            for name in entry_names
        }
        known_count = sum(1 for value in final_map.values() if value == "known")
        gm_only_count = len(final_map) - known_count
        return ClassifyResult(
            known_outline=brief,
            visibilities=final_map,
            known_count=known_count,
            gm_only_count=gm_only_count,
            auto_classified=auto,
        )

    # ------------------------------------------------------------------ 按需检索
    async def recall_canon(
        self,
        bot_id: str,
        query: str,
        recent_turns: list[str] | None = None,
        *,
        decision_context: dict[str, Any] | None = None,
    ) -> WorldRecallResult:
        """对角色可见的设定条目做本地粗筛 + JEV 精筛；JEV 不可用或失败时退回高精本地匹配。"""
        text = str(query or "").strip()
        if not text:
            return WorldRecallResult(reason="empty_query")

        cfg = self.configs(bot_id)
        limit = cfg.int("story.world_recall_limit", 3, lo=0, hi=10)
        if limit <= 0:
            return WorldRecallResult(reason="off")

        entries = await self.store.known_canon(bot_id)
        if not entries:
            return WorldRecallResult(reason="no_known_canon")

        candidates_cap = cfg.int("story.world_recall_candidates", 16, lo=1, hi=60)
        threshold = cfg.float("story.world_recall_threshold", 0.55, lo=0.0, hi=1.0)
        recent = [str(turn).strip() for turn in (recent_turns or [])[-4:] if str(turn).strip()]
        recent_text = " ".join(recent[-2:])

        # 1. 本地打分与粗筛：标题/子词命中的排在前面，剩余名额补齐以便 JEV 识别隐晦指代
        local_scores: dict[str, float] = {}
        ranked_for_pool: list[tuple[float, int, dict[str, Any]]] = []
        for index, entry in enumerate(entries):
            q_score = world.score_entry(entry, text)
            r_score = world.score_entry(entry, recent_text) if recent_text else 0.0
            combined = q_score + 0.35 * r_score
            name = str(entry.get("name") or "")
            local_scores[name] = q_score
            ranked_for_pool.append((combined, index, entry))

        ranked_for_pool.sort(key=lambda item: (-item[0], item[1]))
        chosen = [entry for _, _, entry in ranked_for_pool[:candidates_cap]]

        # 2. JEV 精筛（单次批量 noul 问答）
        if getattr(self.jev, "available", False):
            questions = {
                f"w{index}": {
                    "type": "noul",
                    "instructions": WORLD_RECALL_INSTRUCTION.format(
                        category=world.CATEGORY_LABELS.get(
                            str(entry.get("category") or "other"), "其他"
                        ),
                        name=str(entry.get("name") or "").strip(),
                        body=trim_at_boundary(str(entry.get("body") or "").strip(), 260),
                    ),
                }
                for index, entry in enumerate(chosen)
            }
            base_state: dict[str, Any] = {
                "current_message": text[:500],
                "recent_turns": recent,
            }
            judge_state = (
                {**base_state, "conversation": decision_context}
                if decision_context
                else base_state
            )
            answers = None
            try:
                answers = await self.jev.ask(judge_state, questions)
                if not answers and judge_state is not base_state:
                    answers = await self.jev.ask(base_state, questions)
            except Exception as exc:  # noqa: BLE001 - 判断模型异常退回本地高精匹配
                self.audit.warn("世界常识 JEV 精筛失败，退回本地匹配：%s", exc)
                answers = None

            if answers:
                scores: dict[str, float] = {}
                passed: list[tuple[float, float, int, dict[str, Any]]] = []
                for index, entry in enumerate(chosen):
                    value = (answers.get(f"w{index}") or {}).get("noul")
                    if not isinstance(value, (int, float)):
                        continue
                    prob = float(value)
                    name = str(entry.get("name") or "")
                    scores[name] = prob
                    if prob >= threshold:
                        passed.append((prob, local_scores.get(name, 0.0), index, entry))
                passed.sort(key=lambda item: (-item[0], -item[1], item[2]))
                picked = [entry for _, _, _, entry in passed[:limit]]
                return WorldRecallResult(
                    entries=picked,
                    reason="judged" if picked else "below_threshold",
                    candidates=len(chosen),
                    scores=scores,
                    judged=True,
                )

        # 3. 本地高精降级：只匹配当前 query，且必须过较高阈值防止日常闲聊误触发
        fallback_scored = world.search_scored(
            entries, text, limit, min_score=LOCAL_FALLBACK_MIN_SCORE
        )
        picked = [entry for _, entry in fallback_scored]
        return WorldRecallResult(
            entries=picked,
            reason="local_fallback" if picked else "no_local_hit",
            candidates=len(chosen),
            scores={str(entry.get("name") or ""): score for score, entry in fallback_scored},
            judged=False,
        )

    async def search_known(
        self, bot_id: str, query: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """供 memory_search 工具显式查询角色可见的设定条目。"""
        entries = await self.store.known_canon(bot_id)
        if not entries or limit <= 0:
            return []
        return [
            entry
            for _, entry in world.search_scored(entries, query, limit, min_score=0.05)
        ]


__all__ = [
    "ClassifyResult",
    "WorldKnowledge",
    "WorldRecallResult",
    "format_world_turn_block",
    "render_world_brief",
]
