"""故事模块的提示词：背景模型（主持人）与场景中的她（玩家）。

场景收进记忆时用的整理提示词在 ``memory/prompts.py``（``STORY_EVENT_*``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .oracle import LIKELIHOODS

ME_TAG = "【我】"
HER_TAG = "【她】"


# --------------------------------------------------------------------- 叙述人称
@dataclass(frozen=True, slots=True)
class Narration:
    """背景模型写给她看的叙述用什么人称（配置项 ``story.narration``）。"""

    key: str
    tag: str  # 转录里叙述前的标记
    gm_rule: str  # 给背景模型的人称规则
    hint: str  # 补叙、收尾这些小段落的人称提示
    actor_line: str  # 告诉她叙述是什么


NARRATIONS: dict[str, Narration] = {
    "second": Narration(
        key="second",
        tag="【旁白】",
        gm_rule=(
            "叙述用第二人称，称她为“你”；只写她能感知到的事，不写她的想法、感受和决定，"
            "不替她说话、不替她行动。"
        ),
        hint="用“你”来写",
        actor_line="{tag}写的是我此刻看到、听到、感觉到的；里面的“你”就是我。",
    ),
    "first": Narration(
        key="first",
        tag="【眼前】",
        gm_rule=(
            "叙述用第一人称，以她的口吻写“我”此刻看到、听到、身体上感觉到的事，像她自己的感官记录；"
            "只写感知，不写她的想法、情绪、判断和决定，不替她说话、不替她行动。"
            "这个“我”不是你：用第一人称只是为了让扮演她的人更容易代入。你写的是递到她眼前的感知，"
            "不是你自己的故事，也不是她的日记；她接下来做什么、说什么由她自己写，"
            "叙述里不要出现“我说”“我问”之类她开口说的话。"
        ),
        hint="用“我”来写",
        actor_line="{tag}写的是我此刻看到、听到、感觉到的，是已经发生的事；{me}后面才是我自己想的和做的。",
    ),
    "none": Narration(
        key="none",
        tag="【眼前】",
        gm_rule=(
            "叙述不带人称：不用“你”“我”“她”来指主角，直接写现场——眼前有什么、发生了什么、"
            "别人说了什么做了什么；她的行动带来的结果也这样写（例如“伞买到了”）。"
            "只写她能感知到的事，不写她的想法、感受和决定，不替她说话、不替她行动。"
        ),
        hint="不带人称来写",
        actor_line="{tag}写的是我此刻看到、听到、感觉到的现场；{me}后面才是我自己想的和做的。",
    ),
}
DEFAULT_NARRATION = "first"


def narration(key: Any = None) -> Narration:
    return NARRATIONS.get(str(key or "").strip().lower(), NARRATIONS[DEFAULT_NARRATION])


NARRATOR_TAG = NARRATIONS[DEFAULT_NARRATION].tag

# --------------------------------------------------------------------- 背景模型
_GM_SYSTEM = """你是一个故事的主持人。故事的主角是“她”，由另一位扮演者来演；你掌握这个世界的全部设定，负责告诉她此刻看到、听到、感觉到什么，并裁定她的行动带来什么结果。

你的边界：
- <<person_rule>>
- 其他人物都由你来演，可以说话和行动，但只写她看得到、听得到的部分。
- 她的行动结果有悬念时，你不能自己决定成败：申报可能性，并把“成了”和“没成”两种后续都写出来，由骰子决定用哪一种。
- 世界不围着她转：别人有自己的事，事情不总是顺利，也不总是有戏剧性。平淡的一天就写得平淡。
- 时间与现实同步，按“现在”给出的真实日期和时刻来写。一场只写眼前这一小段，从“现在”起最多十来分钟；不要在叙述里把钟点往后拨。要跳过更长的时间，就收场，让那段时间真的过去。
- 不要为了凑轮数拖着：她没有要做的决定，或者只是在重复同一个状态（睡觉、发呆、赶路），就该收场。
- 她手机（或世界设定里的等价物）那头是现实里真实存在的人，不属于这个世界，也不归你来演。他们说过的话只是话，不等于事实。叙述里不要出现他们的名字，不要有来自他们的新消息、来电或回复，他们也不会出场：他们什么时候找她、说什么，只由现实决定。她要是想联系他们，那不在这一场里发生，让她先搁下，不要写出对方的任何反应。
- 涉及设定时以“世界大纲 / 相关设定 / 你的笔记”为准；里面没有的才可以新编，新编的要在收场时登记。
- 朴素具体，不抒情，不总结，不替她下结论。
- 只输出一个 JSON 对象，不要解释，不要 markdown 围栏。"""


def gm_system(style: Any = None) -> str:
    return _GM_SYSTEM.replace("<<person_rule>>", narration(style).gm_rule)


GM_SYSTEM = gm_system()

_GM_OPEN_TASK_BACKFILL = """# 任务：开场
1. backfill：概括从上一场结束到现在这段空白里她大致做了什么。以状态卡为依据，平淡带过，不安排转折，不超过 120 字，<<hint>>。
2. narration：写出此刻她面前的情境，不超过 200 字，停在需要她自己决定做什么的地方。
3. lookup：想查的东西：设定名、笔记标题、以前场景里的关键词，或场景编号（如 #12）；没有就给空数组。

输出：{"backfill": "", "narration": "", "lookup": []}"""
GM_OPEN_TASK_BACKFILL = _GM_OPEN_TASK_BACKFILL.replace(
    "<<hint>>", NARRATIONS[DEFAULT_NARRATION].hint
)

GM_OPEN_TASK_PLAIN = """# 任务：开场
1. backfill：这次不需要补叙，留空字符串。
2. narration：写出此刻她面前的情境，不超过 200 字，停在需要她自己决定做什么的地方。
3. lookup：想查的东西：设定名、笔记标题、以前场景里的关键词，或场景编号（如 #12）；没有就给空数组。

输出：{"backfill": "", "narration": "", "lookup": []}"""

GM_OPEN_TASK_FIRST = """# 任务：开场（这是故事的第一场）
1. backfill：留空字符串。
2. narration：从世界大纲给出的起点开始，写出此刻她面前的情境，不超过 200 字，停在需要她自己决定做什么的地方。
3. lookup：想查的东西：设定名、笔记标题、以前场景里的关键词，或场景编号（如 #12）；没有就给空数组。

输出：{"backfill": "", "narration": "", "lookup": []}"""

GM_TURN_TASK = """# 任务：裁定她刚才的行动，并写出接下来她感知到的情境
- 只裁定她写出来的那一步。她没写的下一步动作、要说的话、要做的决定，都留给她自己，不要替她接着做下去。
- 结果没有悬念：check 填 null，在 narration 里直接写结果和接下来的情境（不超过 200 字）。
- 结果有悬念：填 check，narration 留空。likelihood 只能是：{likelihoods}。success 和 failure 各写一段完整的后续（各不超过 150 字），两段都要认真写，因为你不知道骰子会选哪一段。
- 先判断这一场该不该结束：事情告一段落、接下来是一段不需要她做决定的平淡时间、或者她这一步没有实质行动，就把 end 设为 true，narration 只写一两句收住。{last_hint}
- lookup：想查的东西：设定名、笔记标题、以前场景里的关键词，或场景编号（如 #12）；没有就给空数组。

输出：{{"check": null, "narration": "", "lookup": [], "end": false}}
check 的格式：{{"what": "她想做成的事", "likelihood": "很可能", "success": "", "failure": ""}}"""

GM_TURN_LAST_HINT = "这已经是本场最后一轮，end 必须为 true。"

GM_CLOSE_TASK = """# 任务：收场
这一场结束了。请给出：
- epilogue：可选的一两句收尾，只写周围的环境和别人的动静（{hint}）；不要让她再做任何动作、说任何话，也不要替她做决定。没有就留空。
- card：她的状态卡。where 是地点短语；doing 是她这一场结束时正在做、并且会持续一阵的事（不带主语的短语，如“写课程论文”），不要替她做她还没做的决定；minutes 是这件事预计还要持续多少分钟；phone 是这期间看手机“方便”还是“不方便”。
- next_wake_minutes：多少分钟后再继续故事（{min_wake}–{max_wake}）。接下来没有波澜，就等这件事结束再继续；步步都有变化，才短一些。预约得越密，额度花得越快。
- canon：这一场里新确立、以后要长期保持一致的设定：某个人是谁、某个地方在哪、某样东西是什么、某条规矩。每条 {{"name": "", "category": "人物/地点/组织/规则/物品/其他", "body": ""}}。这一场发生了什么不算设定（那写进 summary 或 notes）；没有就给空数组。
- notes：你想新增或改写的笔记（暗线、伏笔、其他人此刻在做什么、悬而未决的事、后续打算），每条 {{"title": "", "body": ""}}；body 留空表示删掉这条；没有就给空数组。偶然种子用上了就不必为它写笔记。
- summary：给你自己看的本场梗概，用第三人称“她”，不超过 120 字。

输出：{{"epilogue": "", "card": {{"where": "", "doing": "", "minutes": 60, "phone": "方便"}}, "next_wake_minutes": 60, "canon": [], "notes": [], "summary": ""}}"""


def gm_turn_task(*, last: bool) -> str:
    return GM_TURN_TASK.format(
        likelihoods=" / ".join(LIKELIHOODS),
        last_hint=GM_TURN_LAST_HINT if last else "",
    )


def gm_close_task(*, min_wake: int, max_wake: int, style: Any = None) -> str:
    return GM_CLOSE_TASK.format(
        min_wake=min_wake, max_wake=max_wake, hint=narration(style).hint
    )


def gm_open_task(*, first: bool, backfill: bool, style: Any = None) -> str:
    if first:
        return GM_OPEN_TASK_FIRST
    if not backfill:
        return GM_OPEN_TASK_PLAIN
    return _GM_OPEN_TASK_BACKFILL.replace("<<hint>>", narration(style).hint)


def humanize_gap(ms: int) -> str:
    minutes = max(0, int(ms // 60_000))
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days} 天 {hours} 小时" if hours else f"{days} 天"
    if hours:
        return f"{hours} 小时 {mins} 分钟" if mins else f"{hours} 小时"
    return f"{mins} 分钟"


def gm_context(
    *,
    moment: str,
    gap_ms: int | None,
    tokens: float,
    per_day: float,
    capacity: float,
    title: str,
    outline: str,
    hooks: list[str],
    opening: dict[str, str],
    canon_lines: list[str],
    canon_index: list[str],
    notes: list[dict[str, Any]],
    hidden_notes: int,
    scenes: list[str],
    card: str,
    digest: list[str],
    seed: dict[str, Any],
    last_ending: str = "",
    past: list[str] | None = None,
) -> str:
    """背景模型每次调用都带的上帝视角信息。里面没有她的人格、洞察和私人笔记。"""
    now_lines = [moment]
    if gap_ms is None:
        now_lines.append("这是故事的第一场。")
        start = "，".join(
            part
            for part in (
                f"在{opening['where']}" if opening.get("where") else "",
                f"正在{opening['doing']}" if opening.get("doing") else "",
            )
            if part
        )
        if start:
            now_lines.append(f"起点：她{start}。")
    else:
        now_lines.append(f"距上一场结束：{humanize_gap(gap_ms)}。")
    refill = f"平均每 {1440 / per_day:.0f} 分钟回补 1 次，" if per_day > 0 else "不再回补，"
    now_lines.append(
        f"唤醒额度：剩 {tokens:.1f} 次（{refill}最多攒 {capacity:g} 次）。每演一场花 1 次；"
        "预约间隔比回补间隔短，就是在花积蓄。额度紧张时，把她安排进耗时长、没有波澜的活动；"
        "充足时才展开密集的场景。"
    )
    blocks = ["# 现在\n" + "\n".join(now_lines)]

    world_lines = [title, outline] if title else [outline]
    if hooks:
        world_lines.append("主线钩子：")
        world_lines.extend(f"- {hook}" for hook in hooks)
    blocks.append("# 世界大纲\n" + "\n".join(world_lines))

    canon_block = canon_lines or ["（没有与此刻相关的设定）"]
    if canon_index:
        canon_block = [
            *canon_block,
            "其余设定只列名字，需要细节就把名字放进 lookup：" + "、".join(canon_index),
        ]
    blocks.append("# 相关设定\n" + "\n".join(canon_block))

    if notes:
        note_lines = [f"## {note['title']}\n{note['body']}" for note in notes]
        if hidden_notes > 0:
            note_lines.append(f"（还有 {hidden_notes} 条较早的笔记没有列出）")
        blocks.append("# 你的笔记（她看不到）\n" + "\n".join(note_lines))
    else:
        blocks.append("# 你的笔记（她看不到）\n（还没有笔记）")

    if past:
        blocks.append(
            "# 更早的、和此刻可能有关的场景\n"
            + "\n".join(f"- {line}" for line in past)
            + "\n想看哪一场的完整经过，就把它的编号（如 #12）放进 lookup。"
        )
    if scenes:
        blocks.append("# 最近几场的梗概\n" + "\n".join(f"- {line}" for line in scenes))
    if last_ending:
        blocks.append("# 上一场是这样收住的（接着它往下写，不要重演）\n" + last_ending)
    blocks.append("# 她目前的状态卡\n" + card)
    if digest:
        blocks.append(
            "# 她最近在手机上和人聊过的事\n"
            "（是她的记忆，不等于事实。这些人在现实里，不归你演：叙述里不要出现他们的名字，"
            "也不要写他们又发来了什么）\n"
            + "\n".join(f"- {line}" for line in digest)
        )
    words = "、".join(str(word) for word in seed.get("words") or [])
    blocks.append(
        "# 这次的偶然种子\n"
        f"焦点：{seed.get('focus')}；意象：{words}\n"
        "把它解释成一件具体的事融进这一场；实在用不上，就在收场的 notes 里写一条说明为什么不用。"
    )
    return "\n\n".join(blocks)


def gm_progress(
    transcript: list[dict[str, Any]],
    *,
    backfill: str = "",
    twist: str = "",
    lookups: list[str] | None = None,
    style: Any = None,
) -> str:
    """这一场到目前为止的经过（只含她外显的言行），外加待兑现的转折和查到的设定。"""
    lines: list[str] = []
    if backfill:
        lines.append(f"{narration(style).tag}（补叙）{backfill}")
    rendered = render_transcript(transcript, viewer="gm", style=style)
    if rendered:
        lines.append(rendered)
    blocks = ["# 这一场到目前为止\n" + "\n".join(lines)]
    if twist:
        blocks.append(
            f"# 上一次掷骰\n结果是“{twist}”，{twist[-2:]}还没有体现，请在这次的叙述里体现。"
        )
    if lookups:
        blocks.append("# 你查到的\n" + "\n".join(lookups))
    return "\n\n".join(blocks)


# --------------------------------------------------------------------- 场景中的她
ACTOR_MEMORY_NOTE = """# 关于我的记忆
- 【我的记忆】是我自己亲历或想起的事，只是背景；自然接得上时才让它轻轻影响我，不用主动提。"""

ACTOR_SCENE = """# 我这会儿
- 现在是{moment}。我没有在和谁聊天，这是我自己正在过的生活。
- {narrator_line}
- 我只决定我自己：心里怎么想，接下来做什么、说什么。事情成不成、别人怎么反应，不由我来写。
- 我按自己的性子来，不必每次都做“正确”的事；想歇着、想拖着、想躲开都可以。
- 这会儿没法跟手机那头聊天认识的人说上话；想找他们，就先记在心里，等聊天的时候再说。
- 只输出一个 JSON 对象，不要解释：{{"inner": "我心里怎么想（别人看不到，不超过 60 字）", "action": "我做了什么、说了什么（第一人称，不超过 120 字）"}}"""


def actor_system(
    *, base_card: str, persona: str, ambient: str, moment: str, style: Any = None
) -> str:
    chosen = narration(style)
    pieces = [
        base_card.strip(),
        persona.strip(),
        ACTOR_MEMORY_NOTE,
        ambient.strip(),
        ACTOR_SCENE.format(
            moment=moment,
            narrator_line=chosen.actor_line.format(tag=chosen.tag, me=ME_TAG),
        ),
    ]
    return "\n\n".join(piece for piece in pieces if piece)


def actor_user(
    transcript: list[dict[str, Any]], recall_parts: list[str], style: Any = None
) -> str:
    blocks: list[str] = []
    cleaned = [part.strip() for part in recall_parts if part and part.strip()]
    if cleaned:
        lines = ["【我的记忆，不是眼前发生的；不用主动提】"]
        lines.append(f"我脑子里浮起：{cleaned[0]}")
        lines.extend(f"心里还有一点余波：{part}" for part in cleaned[1:])
        blocks.append("\n".join(lines))
    blocks.append("# 刚才到现在\n" + render_transcript(transcript, viewer="me", style=style))
    blocks.append("现在轮到我。")
    return "\n\n".join(blocks)


# --------------------------------------------------------------------- 转录
def render_transcript(
    transcript: list[dict[str, Any]], *, viewer: str, style: Any = None
) -> str:
    """把场景转录渲染给不同的读者。

    - ``me``：她自己看，带内心；
    - ``gm``：背景模型看，只有外显的言行，不带内心；
    - ``memory``：记忆整理器看，带内心。
    """
    tag = narration(style).tag
    lines: list[str] = []
    for turn in transcript:
        role = turn.get("role")
        text = str(turn.get("text") or "").strip()
        if role == "narrator":
            if text:
                lines.append(f"{tag}{text}")
            continue
        inner = str(turn.get("inner") or "").strip()
        if viewer == "gm":
            if text:
                lines.append(f"{HER_TAG}{text}")
            continue
        body = f"（心里：{inner}）{text}" if inner else text
        if body:
            lines.append(f"{ME_TAG}{body}")
    return "\n".join(lines)
