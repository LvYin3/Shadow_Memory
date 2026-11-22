# 配置详解

返回 [README](../README.md)。调试与排障见 [DEBUG.md](DEBUG.md)。

## 判断模型

### 方式 A：AstrBot 判断 provider（推荐）

1. AstrBot → 模型提供商 → 新增 → 类型选「判断 / System One」；
2. 填 endpoint、key、模型 ID；
3. 插件配置 `decision.provider_id` 手动填这个 provider 的 ID。

AstrBot 的提供商下拉只列对话模型，判断 provider 需要手填。插件会检查它确实有 `ask()`；超时和重试由 provider 控制。

### 方式 B：插件直连兜底

```jsonc
"decision": {
  "provider_id": "",
  "base_url": "https://your-endpoint.example/v1",
  "api_key": "...",
  "model": "your-decision-model",
  "protocol": "auto",
  "timeout_ms": 3000,
  "full_context": true,
  "full_context_max_chars": 16000
}
```

- `protocol=auto`：先试 `/systemone`，404/405/响应不合法再退回 `/chat/completions`。
- `systemone` 返回 `noul` 概率、`choice` 分布；`score` 是 0 基。
- `full_context` 会把 AstrBot 的 system_prompt、文本 history、当前 prompt、extra 文本交给 JEV；不传工具 schema、图片/音频二进制。
- history 从最新一轮向旧取，system 永不截断，最新一轮自身超限时内部截断。
- gate 使用上一轮成功请求快照 + 当前消息；首轮/重启后回退轻量 state。
- 全量 JEV 失败会自动用轻量 state 重试一次。
- **隐私**：`full_context=true` 会把完整人格和历史发到外部判断服务；不需要时关闭。

优先级：provider 可用 → provider；否则直连；都没有 → 静默。

## 群聊门控问题

非 @ 群消息到来时，插件问判断模型一个 noul 问题，概率 ≥ `gate_threshold` 才接话。这句问题可以改：

```jsonc
"interaction": {
  "gate_threshold": 0.5,
  "gate_question": "Given the recent group messages, ... is this message inviting me to respond now? ..."
},
"bot_overrides": {
  "aiocqhttp": { "gate_question": "..." }
}
```

- 整段替换，不是追加；配置项默认预填内置原文，照着改即可。
- **极性**：问题必须写成“概率越高越该回复”。写成“我该不该保持沉默”这类反向问题，`gate_threshold` 的含义会反过来。
- 生效优先级：WebUI「设置」页按机器人的覆盖 → `bot_overrides.<bot_id>.gate_question` → 全局 `interaction.gate_question` → 内置原文。任何一层留空（只有空白也算）都落到下一层。
- 判断模型能看到的 state 字段，写问题时可以引用：
  - `recent_messages`：最近 `gate_history` 条群消息（`speaker` / `seconds_ago` / `text`）；
  - `seconds_since_last`：距上一条群消息的秒数；
  - `my_last_reply_seconds_ago`：我上次在这个群说话距今的秒数，没说过为 `null`；
  - `persona_tendencies`：与当前消息最相关的至多 3 条人格条目；
  - `current_message`：当前消息的 `speaker` / `text`；
  - `conversation`：仅 `decision.full_context=true` 且有上一轮请求快照时才有。
- 全量上下文判断失败后的轻量重试用同一个问题。
- WebUI 文本框预填当前生效的问题；清空或改回配置里的原文再保存 = 删除覆盖、跟随配置。WebUI 覆盖上限 2000 字，存在插件数据库的 `kv` 表里，不随记忆导出/导入/转移。
- WebUI 覆盖每条群消息现读，保存后从下一条消息起生效，不用重载插件。
- **升级提示**：内置原文会作为默认值落盘到你的配置里。以后插件更新了内置问题，已保存的配置不会自动跟着变；想用新版内置问题，把这一项清空即可。
- 当前用的是哪一层，看 DEBUG 日志 `group_gate_detail` 的 `question_source`（`webui` / `bot_override` / `config` / `builtin`）。

## 写作用模型

```jsonc
"text": {
  "provider_id": "",          // 留空用会话默认对话模型
  "base_url": "",             // 只有直连时才需要
  "api_key": "",
  "model": "deepseek-chat",
  "temperature": 0.4,
  "max_tokens": 1200
}
```

碎片、gist、摘要、蒸馏、事实抽取都走它。直连时按 `task` 控制思考模式：

```jsonc
"thinking": "auto",
"reasoning_effort": "",
"thinking_by_task": {
  "fragment": "disabled",
  "tension": "disabled",
  "facts": "disabled",
  "distill": "enabled",
  "consolidate": "enabled"
}
```

走 AstrBot provider 时，`text_chat()` 不会传按调用参数，task 级 thinking 不生效；需要在 provider 的 `custom_extra_body` 全局配置。

## 上下文与 token

**务必修改 AstrBot 的「压缩前最多保留对话轮数」：**

```text
agent_runner.config.compression.max_turns: 20
provider_ltm_settings.group_message_history_enable: false
```

`max_turns=-1` 会让每次工具调用都重发完整历史，token 会迅速放大。

插件自己的上下文注入：

- `interaction.environment`：第一人称场景与 say 规则；
- `recall`：细节召回；
- `ambient`：常驻背景；
- `context`：固定聊天上下文。

## 固定聊天上下文

```jsonc
"context": {
  "recent_messages": 20,
  "max_chars": 2000
}
```

- 插件参与 LLM 请求时补最近 N 条用户/机器人消息；
- `req.contexts` 非空时只补缺失消息；
- `context.recent_messages=0` 关闭；
- AstrBot 自带群聊上下文开启时插件跳过补全，避免重复；
- 每个 `(bot, umo)` 最多保留 200 条。

## 交流环境与 say

```jsonc
"interaction": {
  "environment": {
    "enabled": true,
    "prompt": ""
  },
  "say_example": true,
  "fallback_send_final_text": false
}
```

- 环境块说明“我在哪个平台、和谁说话、必须用 say”；
- 自定义 `environment.prompt` 只替换规则段，动态场景仍自动拼；
- `say_example` 在上下文末尾注入临时 say 工具调用示例，避免模型只回正文；
- `fallback_send_final_text` 默认关：没调用发送工具时最终文本不发送；打开后作为普通消息发出；
- 模型偶发在 `say/speak` 参数末尾带 `</text>`，插件会精确清理，不做全局 HTML 清洗。

## 记忆归属

召回内容是第一人称原文，插件固定做：

- system 中追加“关于我的记忆”说明；
- 召回块以临时 `TextPart` 注入，带“我的记忆/我知道的事”标签；
- 不主动说“我想起/我记得”。

事件入库时区分主语：只有角色自己的行动写“我”；对方的事用名字；共同经历写“我和小明/我们”。

## 常驻背景

```jsonc
"ambient": {
  "enabled": true,
  "recent_days": 7,
  "recent_limit": 5,
  "recent_chat_min": 2,
  "recent_max_chars": 150,
  "people_limit": 5,
  "person_topic_chars": 20,
  "important_limit": 5,
  "important_chars": 20,
  "promote_threshold": 0.75,
  "total_max_chars": 400,
  "min_salience": 3
}
```

- 近期：最近 7 天最多 5 条关闭事件；无足轻重的事件（`salience=1`）不进“最近”；
- `recent_chat_min`：近期里至少留给聊天事件的条数，防止被故事事件占满；`0` 表示不保留；
- 人物：前 5，每人一句最近话题；
- 重要：事件关闭时对 `salience>=min_salience` 问 JEV，过阈值才升入；
- 近期排除 important，重要只出现在“重要”行；
- people/近期首次启用会回填历史关闭事件；important 只从新事件开始。

## 调用频率

```jsonc
"recall": {
  "candidate_pool": 400,
  "skip_smalltalk": true,
  "min_query_chars": 2
},
"persona": {
  "distill_retry_minutes": 360,
  "distill_check_seconds": 300
}
```

- 寒暄/短消息跳过细节召回，常驻背景仍注入；
- `candidate_pool`：从最近多少条还没忘掉的事件里找候选；已经忘掉的不占名额；
- 无候选不调用 JEV；
- 召回主句和张力辅句合并为一个 `ask`；
- post_turn 的 4 个判断合并为一个 `ask`；
- ambient 重要判断保持事件关闭时即时执行；
- 蒸馏失败按分钟数退避，worker 按秒数巡检；
- 夜间任务名为 `shadow-memory-*`，只清理旧的四个 `persona-memory-*` 固定任务，不碰备忘录任务。

## 事件归属与重建

- 未关闭或无 `event_turns` 的事件不可重建；
- 重建只改 `fragment / summary / excerpts / entities`，保留 salience/S/R/status；
- 不重抽事实、不重抽张力、不重建边；
- 接口：`POST /shadow_memory/event/rebuild`，body `{"bot": "...", "event_id": 123}`；
- 新增路由后需重载插件。

## TTS

- 解析顺序：插件 DB `kv.tts_provider_id` → `interaction.tts_provider_ids[bot]` → 无；
- 未绑定 = 不合成语音，`speak` 按文字发出；
- 已绑定但合成失败回退文字；
- QQ 官方平台例外：语音规范化为 24kHz/单声道 WAV 并预检 Tencent SILK，失败只报错，不降级文字；
- WebUI「设置」可按 bot 选择 TTS provider；「静音」写空字符串，「跟随配置」删 kv。

## 人格来源

```jsonc
"persona": {
  "mode": "append",     // append | plugin_only
  "base_card": "",
  "quota_core": 6,
  "quota_edge": 7,
  "quota_tension": 3
}
```

- `append`：AstrBot persona 作基底，插件追加记忆蒸出的人格块；
- `plugin_only`：AstrBot persona 留空，插件使用 `base_card` + 人格块。

按机器人覆盖：

```jsonc
"bot_overrides": {
  "aiocqhttp": {
    "persona_mode": "plugin_only",
    "base_card": "她是一个住在海边小镇的人……",
    "gate_threshold": 0.55,
    "qzone_daily_limit": 1
  }
}
```

## QQ 空间

- 仅 `aiocqhttp`/`napcat` 等第三方框架可用；
- 官方 QQ 平台会自动隐藏 `qzone` 工具；
- 默认 action `send_qzone_msg`；
- `ugc_right` 支持 1/4/64；
- 图片支持公网 URL、图片库文件名、`event:<n>`；
- 每日软上限默认 3。

## 故事模块

让角色在聊天之外有自己的生活：一个「背景模型」当主持人，按现实时间一场一场地推进她的日常；她经历过的事进入记忆，和聊天记忆同表同权。**默认关闭**，目前是第一段「独处的生活」：她不会主动给人发消息，也没有独立的配角。

### 开启步骤

1. 写一份世界种子（格式见下，示例 [world_seed.example.json](world_seed.example.json)），放到 `data/plugin_data/shadow_memory/seed/`；
2. 插件配置里打开 `story.enabled`，重载插件；
3. 私聊发 `/story import <文件名>`（或 WebUI 的导入对话框）；
4. `/story on` 开启当前机器人；`/story status` 看状态和还缺什么；
5. 想马上看效果，用 `/story wake` 手动演一场（不消耗唤醒额度）。

没有世界种子、取不到基础人设、或没有可用的对话模型时，`/story on` 会拒绝并说明缺什么。

### 配置

```jsonc
"story": {
  "enabled": false,            // 总开关；还需按机器人 /story on
  "provider_id": "",           // 背景模型；留空用 AstrBot 默认对话模型
  "actor_provider_id": "",     // 场景中的她；留空用 AstrBot 默认对话模型
  "base_card": "",             // 场景中她的基础人设；留空见「人设来源」
  "refill_per_day": 10,        // 唤醒额度每天回补多少次
  "bucket_capacity": 30,       // 最多攒多少次
  "max_rounds": 4,             // 单次唤醒最多往返几轮
  "min_wake_minutes": 10,      // 两次唤醒的最小间隔
  "max_wake_minutes": 720,     // 最长间隔；背景模型没预约时也按它
  "backfill_min_minutes": 30,  // 空白期超过多久才补叙
  "tick_seconds": 60,          // 巡检间隔
  "retry_minutes": 30,         // 一场失败后多久重试
  "max_failures": 3,           // 连续失败多少次后自动暂停
  "canon_context_items": 8,    // 每次预取给背景模型的相关设定条数
  "recent_scenes": 3,          // 给背景模型看的最近场景梗概条数
  "chat_digest_items": 5,      // 给背景模型看的最近聊天事件条数；0 = 不发
  "notes_max_chars": 2000,     // 每次带给背景模型的主持人笔记总字数
  "card_stale_hours": 6,       // 状态卡过期多久后不再注入聊天
  "chat_boundary": true,       // 聊天时提醒她不把没发生的事说成已发生
  "narration": "second"        // 背景模型写给她看的叙述用什么人称：second / first / none
}
```

`narration` 的三种取值：`second` 称她为“你”（默认）；`first` 以她的口吻写“我”看到听到的事；`none` 不带人称，直接写现场。三种只影响叙述怎么写，不影响记忆（她记下的始终是第一人称）。用 deepseek-chat 各演过两轮、每轮二十场：提示词收紧后三种的越界次数都很低，分不出高下；收紧之前，第一人称下背景模型替她行动、替她说话的次数明显更多。换人称后建议用 `tests/live/smoke_story.py --narration first` 在自己的模型上先看几场。

### 一次唤醒做什么、花多少

1. 背景模型开场：必要时先补叙上一场到现在的空白，再写出她此刻面前的情境；
2. 她给出自己的想法和行动，背景模型裁定并写出接下来的情境，往返最多 `max_rounds` 轮；
3. 结果有悬念时，背景模型只申报可能性并写好「成 / 没成」两种后续，由程序掷骰决定用哪一种；
4. 背景模型收场：写状态卡、登记新设定、更新笔记、预约下次唤醒；
5. 写作模型把这一场整理成她的第一人称记忆，判断模型评显著度，一次性落库。

模型调用次数：背景模型 `轮数 + 2`，她 `轮数`，写作模型 1，判断模型最多 3（开场召回、显著度、是否进常驻背景）。**默认 4 轮时单次唤醒最多 14 次调用，按每天 10 次唤醒算上限约 140 次／天**；场景可以提前收场，实际通常更少。模型输出不是合法 JSON 时该次调用会重试一次。

额度是令牌桶：按时间匀速回补，没用完的留着，到 `bucket_capacity` 封顶。耗尽时故事静默、不报错，攒够一次后继续，中间的空白由下一场开场补叙。关掉再开最多带一天的额度起步。手动唤醒不消耗额度。

一场失败（模型报错、输出不合法）时整场丢弃：不进记忆、不扣额度，`retry_minutes` 后重试；连续 `max_failures` 次失败自动暂停，用 `/story resume` 恢复。

### 世界种子

```jsonc
{
  "version": "world_seed/v1",
  "title": "青屿",                  // 可选，≤40 字
  "outline": "……",                 // 必填，50–3000 字：时代、地点、她的身份与日常、基调、通讯方式
  "entries": [                      // 可选，≤200 条；按需检索的设定
    {"category": "地点", "name": "青屿大学", "body": "……"}   // name ≤40 字，body ≤500 字
  ],
  "hooks": ["……"],                 // 可选，≤10 条主线钩子，每条 ≤200 字
  "opening": {"where": "合租屋", "doing": "赖床"}   // 可选，第一场的起点
}
```

- `category` 取 人物 / 地点 / 组织 / 规则 / 物品 / 其他；
- 种子运行时只读。演出中新确立的事实进「设定集」，只追加，不会改写种子；
- 重新导入种子会换掉大纲、钩子和种子条目，设定集、主持人笔记、场景和状态卡都不动；
- 写种子时请对照她已有的人格和事实（例如她在哪上学），背景模型运行时读不到这些，不会自己对齐；
- 古风或异世界设定要在大纲里写明「远程通讯」的等价物，否则她没法解释手机那头的人。

### 人设来源

场景中的她需要一份基础人设，按顺序取：

1. `story.base_card`（多机器人可用 `bot_overrides.<bot_id>.story_base_card` 分别指定）；
2. `persona.mode = plugin_only` 时的 `persona.base_card`；
3. AstrBot 的默认人格。

AstrBot 内置的通用默认人格（helpful assistant）不算人设。第 3 项取的是全局默认人格；如果你按会话给不同机器人配了不同人格，请用第 1 项显式指定。`/story status` 和 WebUI 会显示实际用的是哪一个。

### 状态卡与聊天

- 每场收场都会留下状态卡：她在哪、在做什么、预计到何时、方不方便看手机；
- 聊天时它并入「交流环境」块的末尾（所以需要 `interaction.enabled` 和 `interaction.environment.enabled`）。回不回消息仍由她自己用 `say` / `silent` 决定，程序不拦截；
- 过了预计结束时间，「不方便看手机」立刻失效，状态卡改说「之前在……，应该已经忙完」；再过 `card_stale_hours` 小时就不再注入；
- `chat_boundary` 会加一句提醒：聊到自己的生活时只说现状、往事和打算，不把没发生的事说成已经发生。这是提示词约束，模型偶尔会越界。

### 隐私

- **发给背景模型 provider 的**：世界种子、设定集、主持人笔记、场景里她说出口和做出来的事、最近聊天事件的碎片与摘要（`chat_digest_items = 0` 可关闭最后一项）；
- **不发给背景模型的**：人格三层、洞察、她的私人笔记与日记、她在场景里的内心活动；
- **发给场景中的她的 provider 的**：基础人设、人格、常驻背景、召回的记忆、场景转录；
- 配了 `provider_id` / `actor_provider_id` 但找不到对应 provider 时，故事会报「没有可用的对话模型」，不会悄悄改用默认模型。

### 不替现实里的人说话

背景模型能从聊天摘要里看到她和谁聊过。如果它在场景里写「某某发来一条消息」，她就会记住一段现实里从没发生过的对话。所以除了提示词里禁止，程序还会硬拦：

- 背景模型写给她看的内容（叙述、补叙、成败两种后续、收尾）里出现了聊天联系人的名字或别名，这次输出作废，带着原因重试一次；仍然如此则整场丢弃；
- 提到这些名字的新设定不会被登记；主持人笔记是背景模型自己看的，可以提到（例如「他说寄了茶，还没到」）；
- 只拦背景模型这一场看得到的名字，单字的名字不拦；联系人与世界种子里的人物同名时，以世界里的人物为准；
- 代价：背景模型不能写「小明寄的茶到了」，只能写「之前说好寄来的那盒茶到了」。昵称是日常词（比如「今天」）的联系人会导致频繁误拦，这时把 `chat_digest_items` 设为 0，背景模型就不再看到任何聊天内容。

### 与记忆系统的关系

- 场景写进 `events`，`source = story`；召回、常驻背景、遗忘、夜间巩固、人格蒸馏与聊天事件同权；
- 补叙单独一条事件，显著度固定为 1；
- 第一段的场景不产生张力和事实候选；
- `/memory export full` 和记忆转移只带记忆事件（含来源标记），不含世界种子、设定集、主持人笔记和场景转录；后者用 WebUI 故事页签的「导出故事数据」。

### 已知限制

- 故事事件会参与人格蒸馏，只靠显著度约束；建议开启后隔几天对比人格版本（`/persona rollback` 可回滚）；
- 显著度 ≥ `ambient.min_salience` 的故事事件也会参与常驻背景「重要」的晋升，和聊天事件竞争同样的名额（「最近」有 `recent_chat_min` 保底，「重要」没有）；
- 只识别公历固定日期的节日，农历节日不识别；
- WebUI 不展示完整转录、设定集和主持人笔记（会剧透），需要时导出；
- 没有主动消息、`act` 工具和独立配角，这些在后续阶段。

## seed / 导入

- `skills/persona-seed/` 给外部 LLM 用，把口述过去整理成 `persona_seed v1`；
- 文件放 `data/plugin_data/shadow_memory/seed/`；
- `/memory import <文件> [force]` 导入；
- 非空库必须 `force`，导入前自动备份。
