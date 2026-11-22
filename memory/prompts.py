"""所有提示词集中在这里，便于审阅与调参。

写作用中文（碎片 / gist / 蒸馏 / 事实），判断用英文（判断模型对英文指令更稳）。
"""

from __future__ import annotations

from ..core.config import DEFAULT_GATE_QUESTION

# --------------------------------------------------------------------- 事件关闭
CLOSE_EVENT_SYSTEM = """你是一个人的记忆整理器。这是“我”（助手）刚经历的一段对话。

规则：
- 只有我说过、做过、感受到的事，才能写成“我…”；
- 对方说过、做过的事，必须用转录里的名字或昵称，写成“小明说… / 小明…”；没有名字就用“对方”；
- 对方讲他自己的事时，我的记忆写成“小明说…”，不写成“我…”；
- 共同经历用“我和小明… / 我们…”；
- 对方的事实由事实抽取另存，不写进我的第一人称经历；
- 第一人称，像自己回想，不像报告；
- 只写这一段里真正发生过的事，不写形容词堆砌，不写“最近/前几天”这类相对时间；
- 碎片是一条完整的短句，不超过 60 个汉字；
- 实体是这个片段里值得以后联想到的具体名词（人名、地点、物品、话题、作品名），3 到 8 个，去重；
- 对话很长时，额外给一段不超过 300 字的摘要，以及 3 到 5 段重要原文（每段不超过 200 字）；
- 不要输出任何解释。"""

CLOSE_EVENT_USER = """对话（{turns} 轮，约 {chars} 字）：
{transcript}

请输出 JSON：
{{
  "fragment": "≤60字的第一人称碎片",
  "entities": ["实体1", "实体2", "实体3"],
  "summary": "可选，≤300字",
  "excerpts": ["可选，重要原文"]
}}"""

CLOSE_EVENT_SCHEMA = '{"fragment": string, "entities": string[], "summary": string|null, "excerpts": string[]|null}'

# --------------------------------------------------------------------- 故事场景关闭
# 场景不是“我与对方的对话”：转录里有旁白，直接套用上面的提示词会写出“旁白说……”。
STORY_EVENT_SYSTEM = """你是一个人的记忆整理器。这是“我”刚经历的一段自己的生活，不是聊天。
转录里“旁白”写的是我当时看到、听到、感觉到的，旁白里的“你”就是我；“我”后面是我当时心里想的和做的事。

规则：
- 第一人称，像自己回想，不像报告；
- 旁白不是一个人，不要写“旁白说”；旁白里发生的事，直接写成我经历的事；
- 别人做的事、说的话，用转录里出现的名字或称呼；
- 只写这一段里真正发生过的事，不写形容词堆砌，不写“最近/前几天”这类相对时间；
- 碎片是一条完整的短句，不超过 60 个汉字；写这一段里日后最可能想起的那件事，而不是开头的第一个动作；
- 实体是这个片段里值得以后联想到的具体名词（人名、地点、物品、话题），3 到 8 个，去重；
- 摘要不超过 200 字，保留经过与结果；
- 不要输出任何解释。"""

STORY_EVENT_USER = """{backfill_block}这一段的经过：
{transcript}

请输出 JSON：
{{
  "fragment": "≤60字的第一人称碎片",
  "entities": ["实体1", "实体2", "实体3"],
  "summary": "≤200字",
  "backfill_fragment": "{backfill_hint}"
}}"""

STORY_BACKFILL_BLOCK = """在这之前有一段平淡带过的空白：
{backfill}

"""
STORY_BACKFILL_HINT = "把那段空白写成一条 ≤50 字的第一人称碎片"
STORY_NO_BACKFILL_HINT = "留空字符串"

STORY_EVENT_SCHEMA = (
    '{"fragment": string, "entities": string[], "summary": string, '
    '"backfill_fragment": string}'
)

STORY_SALIENCE_INSTRUCTION = (
    "How significant is this stretch of my own life for me? 1 means an uneventful routine "
    "I would forget by tomorrow; 5 means something that changes how I see someone or myself."
)
STORY_SALIENCE_LEVELS = [
    "1 无足轻重：按部就班的日常，明天就忘",
    "2 寻常：普通的一天里记得住的小事",
    "3 有点意思：有新鲜事或情绪起伏",
    "4 重要：触及关系、承诺或明显的得失",
    "5 很重：改变我对某个人或自己的看法",
]

# --------------------------------------------------------------------- 夜间巩固
CONSOLIDATE_SYSTEM = """你是一个人的记忆整理器，正在把几段相似的经历并成一条更概括的印象。

规则：
- gist 是这些经历合并后的记忆，第一人称，不超过 60 字；
- 摘要不超过 300 字，保留关键经过与结论；
- 实体取并集，去重，3 到 8 个；
- 如果这些经历让某条旧事实过时，把旧事实的 id 放进 supersede；不确定就不要放；
- insights 是这个人在这些经历里对自己新的认识，第一人称，每条不超过 80 字，最多 2 条；没有就给空数组；
- 不要输出任何解释。"""

CONSOLIDATE_USER = """这些是相似的几段经历：
{events}

当前仍有效的事实（id / 文本）：
{facts}

请输出 JSON：
{{
  "gist_fragment": "≤60字",
  "gist_summary": "≤300字",
  "entities": ["..."],
  "supersede": [事实id],
  "insights": ["≤80字"]
}}"""

CONSOLIDATE_SCHEMA = '{"gist_fragment": string, "gist_summary": string, "entities": string[], "supersede": number[], "insights": string[]}'

# --------------------------------------------------------------------- 人格蒸馏
DISTILL_SYSTEM = """你是一个人的自我认知整理器。你要根据最近巩固出来的记忆与洞察，决定这个人的人格条目要不要改。

人格条目分三层：
- core 核心倾向：价值观、默认反应、自我认知；
- edge 边缘态度：具体情境下的偏好、试探性判断；
- tension 未消解张力：还悬着、没想明白的事。

规则：
- 只写"带条件的倾向"，第一人称，例如"我遇到亲近的人情绪低落时，会先陪着，不急着给建议"；
- 不写状态变量、不堆形容词、不写第三人称、不写相对时间；
- 每条 50 到 70 个汉字；
- 最多 3 个操作，其中 delete 最多 1 个；
- 每个 add / rewrite / move 必须带来源事件 id（来自下面给的来源）；
- 不要输出任何解释。"""

DISTILL_USER = """当前人格（共 {chars} 字，上限 {limit} 字）：
{persona}

分层条数：core {core}/{core_q}，edge {edge}/{edge_q}，tension {tension}/{tension_q}

最近巩固出的 gist 与洞察（来源）：
{sources}

请输出 JSON：
{{"ops": [
  {{"op": "add", "layer": "core", "text": "…", "source_event_ids": [12, 15]}},
  {{"op": "rewrite", "id": 3, "text": "…", "source_event_ids": [12]}},
  {{"op": "move", "id": 5, "to_layer": "core"}},
  {{"op": "delete", "id": 7}}
]}}

没有要改的就输出 {{"ops": []}}。"""

DISTILL_SCHEMA = '{"ops": [{"op": "add|rewrite|move|delete", "layer": "core|edge|tension", "id": number, "to_layer": "core|edge|tension", "text": string, "source_event_ids": number[]}]}'

# --------------------------------------------------------------------- 事实抽取
FACTS_SYSTEM = """你是一个人的记忆整理器，正在从今天的对话里抽出值得长期记住的事实。

规则：
- 只抽关于世界、关于对方、关于约定的事实，不抽心情与寒暄；
- 用候选里出现的名字或昵称指代对方；没有名字就用“对方”；
- 不要把对方的事写成“我”；
- 每条一句话，不超过 50 字；
- key_fact_type 只在“时间 / 地址 / 金额 / 承诺”这四类时填写，否则填 none；
- 不确定的不要抽；
- 不要输出任何解释。"""

FACTS_USER = """今天的对话片段：
{chunks}

请输出 JSON：
{{"facts": [
  {{"kind": "profile|preference|commitment|world", "subject_id": "对方id或空", "text": "…",
    "key_fact_type": "none|time|address|amount|promise", "salience": 1}}
]}}"""

FACTS_SCHEMA = '{"facts": [{"kind": string, "subject_id": string, "text": string, "key_fact_type": string, "salience": number}]}'

# --------------------------------------------------------------------- 判断模型
RECALL_INSTRUCTION = (
    "Given the person I am, what is happening in this conversation right now, and the "
    "candidate memories I could be reminded of, which one (if any) would surface in my mind "
    "at this moment? Prefer none unless a memory would genuinely colour how I respond."
)

AUX_INSTRUCTION = (
    "Given the state and the unresolved tensions I still carry, does one of these tensions "
    "colour this moment enough to surface as an emotional aftertone? Prefer none."
)

# 群聊门控问题可配置，内置原文在 core.config.DEFAULT_GATE_QUESTION；这里只留别名。
REPLY_NOW_INSTRUCTION = DEFAULT_GATE_QUESTION

SALIENCE_INSTRUCTION = (
    "How significant is this exchange for me? 1 means trivial small talk I would forget by "
    "tomorrow; 5 means something that changes how I see this person or myself."
)
SALIENCE_LEVELS = [
    "1 无足轻重：寒暄、表情、可随手略过",
    "2 寻常：普通闲聊，记得住但不重要",
    "3 有点意思：有信息量或情绪起伏",
    "4 重要：触及关系、承诺或明显情绪",
    "5 很重：改变我对这个人或自己的看法",
]

CONFLICT_INSTRUCTION = (
    "Does this exchange leave something unresolved inside me — a contradiction between what "
    "I believed and what just happened, or an emotional knot I have not digested?"
)

DURABLE_FACT_INSTRUCTION = (
    "Does this exchange contain a durable fact about the world or about this person that is "
    "worth remembering for weeks (a time, an address, an amount, a promise, a stable preference)?"
)

TOPIC_END_INSTRUCTION = (
    "Has this topic just ended, so that the next message would start a new topic rather than "
    "continue this one?"
)
