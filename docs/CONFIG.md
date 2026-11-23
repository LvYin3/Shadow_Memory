# 配置详解

返回 [README](../README.md)。调试与排障见 [DEBUG.md](DEBUG.md)。

## 在 WebUI 里改

插件 WebUI 的「配置」页列出这份 schema 里的全部项，最外层按模型、记忆、互动、故事、日志、按机器人分成六个模块卡片，点进去按分组配置，返回不会丢草稿，点「保存」一次性写回 AstrBot 正在用的那一份插件配置，下一轮就生效，不用重载插件。

AstrBot 自带的配置页还在，写的是同一份。两边后保存的那次为准。

「设置」页不在这里。它只管按机器人、存在数据库里的更高一层：TTS、群聊门控问题、固定上下文条数。配置页上这三项旁边有提示。

密钥框不显示原文。留空表示不改；要清掉，勾「清除」。

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

让角色在聊天之外有自己的生活：一个「背景模型」当主持人，按现实时间一场一场地推进她的日常；她经历过的事进入记忆，和聊天记忆同表同权。**默认关闭**。故事与聊天双向接通（她可以在场景里给人发消息，聊天里答应的事用 `act` 交进故事），也支持拥有独立记忆的常驻配角和按故事树组织的长线脉络。

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
  "min_wake_minutes": 0,       // 两次唤醒的最小间隔；0 表示不限制
  "max_wake_minutes": 0,       // 最长间隔；0 表示不限制。没写下次时间时不会拿它兜底
  "share_per_day": 20,         // 自发分享每天回补多少条
  "share_capacity": 20,        // 自发分享最多攒多少条
  "share_per_round": 0,        // 一轮行动里最多主动发几条；0 表示不限制
  "share_cooldown_minutes": 0, // 同一会话冷却；0 表示不冷却。打开后回告和补回也受它约束
  "quiet_hours": "",           // 如 23:00-08:00；留空不设。打开后回告和补回也不发
  "share_groups": "",          // 允许自发分享的群显示名，逗号分隔；留空则群里不自发分享
  "unread_keep": 1,            // 没回的私聊只留最新几条
  "remote_threshold": 0.6,     // 寄东西 / 点外卖 / 转账要多有把握才记账
  "backfill_min_minutes": 30,  // 空白期超过多久才补叙
  "tick_seconds": 60,          // 巡检间隔
  "retry_minutes": 30,         // 一场失败后多久重试
  "max_failures": 3,           // 连续失败多少次后自动暂停
  "canon_context_items": 8,    // 每次预取给背景模型的相关设定条数
  "recent_scenes": 3,          // 给背景模型看的最近场景梗概条数
  "past_scenes": 3,            // 开场最多再带几条更早的相关场景；0 = 不带
  "past_candidates": 30,       // 挑更早场景时先粗筛出的候选数
  "past_threshold": 0.6,       // 判断模型认为“有用”的概率达到多少才带上
  "chat_digest_items": 5,      // 给背景模型看的最近聊天事件条数；0 = 不发
  "notes_max_chars": 2000,     // 每次带给背景模型的主持人笔记总字数
  "card_stale_hours": 6,       // 状态卡过期多久后不再注入聊天
  "chat_boundary": true,       // 聊天时提醒她不把没发生的事说成已发生
  "narration": "first",        // 背景模型写给她看的叙述用什么人称：first / second / none
  "guard_real_names": false,   // 默认关。打开后拦联系人真名和引号原话
  "npc_active_max": 3,         // 同时活跃的配角上限
  "npc_idle_days": 14,         // 多少天没出场就自动休眠
  "spoiler_heavy": false,      // 故事页是否显示笔记和配角记忆
  "thinking": "close"          // 背景模型什么时候开思考：off / close / scene / plan
}
```

`narration` 的三种取值：`first` 以她的口吻写“我”看到、听到的事（默认）；`second` 称她为“你”；`none` 不带人称，直接写现场。三种只影响叙述怎么写，不影响记忆（她记下的始终是第一人称）。第一人称下，提示词会向背景模型说明这个“我”不是它自己，用第一人称只是为了让扮演她的模型更容易代入；叙述里出现“我说：“……””这类替她开口的句子会被拦下重写。用 deepseek-chat 对照过第一人称和第二人称，各演 20 场：越界都很少，第一人称没有比第二人称更多（复核后第一人称 0 处实质越界、2 处顺手的小动作，第二人称 1 处替她做了起床的决定）。换人称后建议用 `tests/live/smoke_story.py --narration second` 在自己的模型上先看几场。

`story.guard_real_names` 默认关。关掉时，背景模型写到联系人的名字，或把他们的话放进引号，都不会整段作废；第一人称下替她开口仍然会拦。打开后恢复原来的拦截：上下文里出现过的联系人不能写进叙述，未了结的远方动作可以点名，引号里的原话整段作废。

`story.npc_active_max` 默认 3，`story.npc_idle_days` 默认 14。配角由背景模型用工具提名，人格只交三层、每条 40 到 90 字，和她的关系写成一句话。建号要等这场提交；这场失败就当没提名。同时活跃满了就拒绝新的提名，要先点名让谁休眠，空位下一场才能用。14 天没在已提交的场里单独出场，开场前自动休眠。休眠的人被写进 `present` 且当时还有空位，会当场恢复并补一次衰减。概览不列出配角；故事页只显示活跃配角的名字和上次出场。

`story.spoiler_heavy` 默认关。故事页里的完整转录、设定集和世界种子需要当场确认才展开，确认不写入浏览器。笔记和配角记忆只在这个开关打开后出现，笔记可以改、可以删。世界种子用表单整份替换，校验失败不写入；设定集只读。导出和日志不经过这道闸。

世界种子也可以用 `skills/world-seed/` 在戏外起稿：先问基调，可以补日常细节，不编人物关系、承诺和规矩。贴了人格种子或事实时不得与之矛盾。技能不创建配角。

背景模型可以用 AstrBot 里已经配好的网页搜索（`provider_settings.web_search` 和 `websearch_provider`），以及那一家自带的打开网页。插件不再另配一把钥匙。没开搜索就不挂工具，场景和以前一样。搜到的原文只留在这一轮背景模型的上下文里，不进转录，不进记忆。每步最多两轮；搜不到、工具出错，或两轮之后还在要工具，都按没搜到处理，这一场不因此失败。她扮演的那一侧没有搜索。收场若开了思考，先把这两轮跑完，再把资料交给直连去写收场。

背景模型什么时候开思考，由 `story.thinking` 决定。`close`（默认）只在收场开；`scene` 先判断这场有没有没收尾的事，有就把整场开开；`plan` 用同一次判断，有就先写一场约束，执行本身不开；`off` 全程不开。思考走写作模型的直连地址（`text.base_url` / `text.api_key` / `text.model`）。只配了 AstrBot 对话模型、没有直连地址时，思考会被跳过，日志里有一条 `thinking_skipped`，这一场就当没开。

对照过四种切法：两种起点（第一场，以及一段带着账单、欠款、约定上门的 30 场历史），每种 6 场，复杂起点和「全程不开」做盲比。只在收场开思考：合理 5 胜 1 负，后果 5 胜 1 负，加戏不比对照多，平淡场不会误开。整场开思考反而更差（合理 2 胜 4 负，加戏更多）。先写约束再演，和不开打成 3 比 3。所以默认是 `close`。收场本来就要调用一次，开思考不加调用次数，只是这一次更慢、输出更长。

### 一次唤醒做什么、花多少

1. 背景模型开场：必要时先补叙上一场到现在的空白，再写出她此刻面前的情境；
2. 她给出自己的想法和行动，背景模型裁定并写出接下来的情境，往返最多 `max_rounds` 轮；
3. 结果有悬念时，背景模型只申报可能性并写好「成 / 没成」两种后续，由程序掷骰决定用哪一种；
4. 背景模型收场：写状态卡、登记新设定、更新故事树、预约下次唤醒；
5. 写作模型把这一场整理成她的第一人称记忆，判断模型评显著度，一次性落库。

模型调用次数：背景模型 `轮数 + 2`，她 `轮数`，写作模型 1，判断模型最多 4（她的开场召回、显著度、是否进常驻背景等）。**默认 4 轮时单次唤醒最多约 15 次调用，按每天 10 次唤醒算上限约 150 次／天**（有配角同场时按出场人数增加）；场景可以提前收场，实际通常更少。模型输出不是合法 JSON 时该次调用会重试一次。

背景模型记得的以前的事：由一棵**故事树**组织长线脉络（每条线挂关联人物、设定、种子条目和场次编号；没挂上任何线的归入「日常」，只给最近 `recent_scenes` 场保留一行梗概，更早的只留场号）。场景里背景模型可以用工具读写主持人笔记（`read_story_notes` / `write_story_note`，每场最多改一次笔记），也可以在 `lookup` 里写 `#编号` 看那一场的完整经过（只有她外显的言行）、写设定名查设定正文、写关键词翻以前场景的梗概。`past_scenes` / `past_candidates` / `past_threshold` 与 `notes_max_chars` 为兼容保留的配置项，当前开场以故事树和按需工具代替预取。

额度是令牌桶：按时间匀速回补，没用完的留着，到 `bucket_capacity` 封顶。耗尽时故事静默、不报错，攒够一次后继续，中间的空白由下一场开场补叙。关掉再开最多带一天的额度起步。手动唤醒不消耗额度。

`min_wake_minutes` 和 `max_wake_minutes` 默认都是 0，表示不夹紧。背景模型写 0 就是这场一结束就可以继续。没写、写空或写成负数时，再单独问它一次下次时间；这一次还是没有，就把 `next_wake_at` 留空，不再自动醒，手动 `/story wake` 和 `act` 的提前唤醒仍然可用。状态卡上「不方便」最长按 7 天计，避免不设上限时一直不看手机。

### 和聊天接通

聊天里她可以用工具 `act(intent, now)` 把一件要做的事交进故事。`now` 为 false 只排队，下一场开场会看到。`now` 为 true 时，如果还有唤醒额度、当前没在演、并且原定的下次唤醒更晚，就把预约提前到现在；这一步不另扣额度，真正演成的那场仍扣一次。没额度时只排队，并回她一句「我现在走不开，先记着。」正在演则回「我这会儿正忙着，先记着，忙完再去。」

她在场景里的行动 JSON 可以带 `messages`，每条是 `to`（先写显示名）和 `text`。对上会话目录里唯一的显示名就发出；没有这个人，第一人称退回，不把目录倒出来；重名则退回这些会话号，这场还能再用会话号发一次。只有对不上人才多给一轮，额度、白名单、安静时段和发送失败不多给。发出去走 `context.send_message`，成功的那句才进聊天记忆。

程序给每条消息分类，不由她或背景模型标注：这场已经裁定了指向这个会话的意图，就是回告；这个会话还在未回名单上，就是补回；其余是自发分享。只有自发分享扣 `share_per_day` / `share_capacity` 那只桶。回告和补回默认不扣，但安静时段和会话冷却一旦打开，三种都拦。群里的自发分享还要群的显示名写在 `share_groups` 里。发出失败会把已经扣的自发额度退回，并且不写聊天记忆。

她在私聊里选择沉默时，这句话记成未回，只保留最新 `unread_keep` 条，更早的删掉，不能再补。下一场开场把还没裁定的意图、留下的未回、以及还没了结的远方动作一起给她和背景模型。补回真正发出后，这条未回才删掉。

她会看到的消息（全部私聊，以及过了门控或 @ 了她的群消息）交给判断模型，只认三件事：寄东西、点外卖、转小钱。最高的一项达到 `remote_threshold` 才记账；打平、失败或不达标都不记。记的是「对方这么说了」，不是事实。没了结之前，背景模型可以在叙述里写出这个显示名；用引号替这个人写原话仍然整段作废。设定集仍然不登记这些名字。收场时背景模型用 `remote` 标成到了、没到或她没管；没标的继续开着。群聊门控的判断状态里多一行她此刻的状态卡，不改默认的门控问题。

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

AstrBot 内置的通用默认人格（helpful assistant）不算人设。第 3 项会先按机器人在 AstrBot 里的兜底配置文件路由（`bot:*:*` / `bot::`）读取默认人格，若仍是内置助手再回退尝试私聊通配路由（`bot:FriendMessage:*`）和全局 `default` 配置。`/story status` 和 WebUI 会显示实际用的是哪一个。

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

背景模型能从聊天摘要里看到她和谁聊过。第一人称下替她开口说话（如“我说：‘……’”）始终会被拦下重写；此外当打开 `story.guard_real_names = true`（默认关）时，还会进一步硬拦现实联系人的名字和引号原话：

- 背景模型写给她看的内容（叙述、补叙、成败两种后续、收尾）里出现了聊天联系人的名字或别名，这次输出作废，带着原因重试一次；仍然如此则整场丢弃；
- 提到这些名字的新设定不会被登记；主持人笔记是背景模型自己看的，可以提到（例如「他说寄了茶，还没到」）；
- 只拦背景模型这一场看得到的名字，单字的名字不拦；联系人与世界种子里的人物同名时，以世界里的人物为准；
- 代价：打开后，除了未了结的远方动作外，背景模型不能直接写「小明寄的茶到了」，只能写「之前说好寄来的那盒茶到了」。昵称是日常词（比如「今天」）的联系人会导致频繁误拦，这时关回 `guard_real_names` 或把 `chat_digest_items` 设为 0。

### 与记忆系统的关系

- 场景写进 `events`，`source = story`；召回、常驻背景、遗忘、夜间巩固、人格蒸馏与聊天事件同权；
- 补叙单独一条事件，显著度固定为 1；
- 故事场景不产生未消解张力和聊天事实候选；
- `/memory export full`（`shadow_memory/full/v3`）与 WebUI「替换」模式的跨机器人转移都会一并携带故事世界（世界种子、设定集、故事树、主持人笔记、场景记录、活跃/休眠配角及其独立记忆）；单独导出故事档案也可用 WebUI 故事页签的「导出故事数据」。

### 已知限制

- 故事事件会参与人格蒸馏，只靠显著度约束；建议开启后隔几天对比人格版本（`/persona rollback` 可回滚）；
- 显著度 ≥ `ambient.min_salience` 的故事事件也会参与常驻背景「重要」的晋升，和聊天事件竞争同样的名额（「最近」有 `recent_chat_min` 保底，「重要」没有）；
- 只识别公历固定日期的节日，农历节日不识别。

## seed / 导入

- `skills/persona-seed/` 给外部 LLM 用，把口述过去整理成 `persona_seed v1`；
- 文件放 `data/plugin_data/shadow_memory/seed/`；
- `/memory import <文件> [force]` 导入；
- 非空库必须 `force`，导入前自动备份。
