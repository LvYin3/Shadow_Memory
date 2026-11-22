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

- 近期：最近 7 天最多 5 条关闭事件；
- 人物：前 5，每人一句最近话题；
- 重要：事件关闭时对 `salience>=min_salience` 问 JEV，过阈值才升入；
- 近期排除 important，重要只出现在“重要”行；
- people/近期首次启用会回填历史关闭事件；important 只从新事件开始。

## 调用频率

```jsonc
"recall": {
  "skip_smalltalk": true,
  "min_query_chars": 2
},
"persona": {
  "distill_retry_minutes": 360,
  "distill_check_seconds": 300
}
```

- 寒暄/短消息跳过细节召回，常驻背景仍注入；
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

## seed / 导入

- `skills/persona-seed/` 给外部 LLM 用，把口述过去整理成 `persona_seed v1`；
- 文件放 `data/plugin_data/shadow_memory/seed/`；
- `/memory import <文件> [force]` 导入；
- 非空库必须 `force`，导入前自动备份。
