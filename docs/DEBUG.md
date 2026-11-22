# 调试与排障

返回 [README](../README.md)。配置项见 [CONFIG.md](CONFIG.md)。

## 日志开关

插件配置 →「日志与审计」：

```jsonc
"logging": {
  "debug": true   // 默认 false；只影响 DEBUG，普通 INFO 不变
}
```

AstrBot 日志级别也调到 DEBUG。常用日志：

- `environment_injected`：环境块注入；
- `ambient_injected`：常驻背景；
- `recall_inject`：召回内容；
- `persona_inject_*`：人格注入；
- `llm_request_budget`：本次请求上下文大小；
- `group_gate` / `group_gate_detail`：群聊门控结果与概率；
- `outbound_text_cleaned`：发送文本清理命中；
- `decision_context_*`：JEV 是否拿到完整上下文。

## Token 预算

```text
[mem] llm_request_budget {
  "contexts": ...,
  "context_chars": ...,
  "tool_result_chars": ...,
  "system_chars": ...,
  "schema_bytes": ...,
  "extra_parts_chars": ...,
  "prompt_chars": ...,
  "tools": ...
}
```

- 插件自身注入通常只有几千字符；
- `context_chars` 很大时先查 AstrBot 的 `max_turns`；
- `tool_result_chars` 大时查 `get_group_message_history`、网页搜索、文件读取等工具结果；
- 图片 token 不计入 `context_chars`，需要单独看模型账单。

## 模型只回正文，不调用 say

1. 确认对话模型「模型能力」勾选了 **工具调用 / tool_use**；
2. 看 `provider_tool_use_disabled` WARN；
3. 看 `llm_response` 的 `tools` 是否为空；
4. 没调用 say 时默认不会发送最终文本；需要保底就设 `fallback_send_final_text=true`；
5. 模型偶发在参数末尾写 `</text>`，插件会精确清理并记 DEBUG。

## WebUI 打不开 / 路由 404

- 修改 `main.py` 或新增视图后必须**重载插件**，只刷新页面不够；
- 看 `webui_routes` 日志；
- 确认 AstrBot ≥ 4.29.3；
- 如果接口存在但页面无反应，检查浏览器控制台和页面静态资源是否需要硬刷新。

## 官方群聊没有日志 / 插件不响应

先确认插件是否被 AstrBot 的 profile 白名单分发：

```json
"plugin_set": ["builtin_commands", "astrbot", "...", "shadow_memory"]
```

官方机器人若不在 `plugin_set`，插件 loaded 但 `on_message` 不会执行。AstrBot 的 `plugin_handlers` / `on_message_seen` DEBUG 可确认。

## 群聊门控 DEBUG

- INFO：`group_gate {"result":"reply|silent"}`，一条结果；
- DEBUG：`group_gate_detail` 概率/阈值/reason/延迟，以及 `question_source`（本次门控问题来自 `webui` / `bot_override` / `config` / `builtin`，见 [CONFIG.md](CONFIG.md#群聊门控问题)）；
- DEBUG：`on_message_seen`、`group_gate_skip` 入口与跳过原因；
- 没有判断 provider 时不主动回复，只保留 @/原生唤醒。

## QQ 语音

- QQ 官方要求 Tencent SILK；
- 插件仅对 `qq_official` 做 24kHz/单声道 WAV 规范化和 SILK 预检；
- 预检失败只记 WARN，不降级为文字；
- 若 `处理语音时出错`，看 `speak_silk_precheck_failed` / `官方语音` WARN 的真实异常；
- NapCat/aiocqhttp 不走这套规范化。

## QQ 空间

- 仅 `aiocqhttp`/`napcat` 可用；官方 QQ 平台工具自动隐藏；
- 检查 `media_tool` 日志的 `qzone active`；
- 常见失败：action 名不是 `send_qzone_msg`、无权限、图片 URL 不可公网访问、每日上限。

## 备忘录取上下文

AstrBot cron 唤醒不触发 `on_llm_request`，所以备忘轮到点时靠 `memo(action="get")` 现场取：

- 提醒内容、现在、人格、会话目录、最近对话、第一人称环境；
- 不会自动走普通请求的召回注入。

## 故事模块

配置与用法见 [CONFIG.md](CONFIG.md#故事模块)。先看 `/story status`（或 WebUI「故事」页签），它会直接列出「还缺」什么。

**故事不动时依次检查：**

1. `故事：总开关未开` → 配置里的 `story.enabled`，改完要重载插件；
2. `故事：未开启` → `/story on`；被拒绝时提示里写了缺什么（世界种子、基础人设、对话模型）；
3. `故事：已暂停（连续 3 次失败……）` → 看下面的失败原因，处理后 `/story resume`；
4. `唤醒额度` 不足 1 → 正常静默，等回补；`下次唤醒` 会显示攒够的时间；
5. `下次唤醒` 还没到 → 背景模型上一场预约的时间；想马上演用 `/story wake`；
6. 巡检每 `story.tick_seconds` 秒一次，插件启动后约 20 秒才开始。

**一场没演成：**

- WebUI「最近的场景」和 `/story status` 的「最近一场」会显示失败原因；
- 常见原因：`输出不是 JSON 对象`（模型没按格式输出，可以换一个更听指令的模型）、`输出缺少必填内容`（没给叙述、行动或状态卡）、`调用失败`（网络或限流）；每次调用失败会先重试一次；
- 失败的场景不进记忆、不扣额度，已经走到哪一步留在 `story_scenes.transcript` 里（导出故事数据可见）。

**日志：**

- INFO：`story_scene`（一场演完：轮数、调用次数、掷骰次数、显著度、新增设定、下次唤醒间隔、剩余额度）、`story_wait_tokens`、`story_enabled` / `story_disabled` / `story_paused` / `story_resumed`、`story_seed_imported`；
- WARN：`故事场景失败`、`故事模型调用失败`、`故事模型输出不是 JSON 对象`、`故事模型输出不合格`（后面带原因，例如叙述里出现了现实联系人的名字）、`故事模块配置的 provider 不可用`；
- ERROR：`故事连续 N 次失败，已自动暂停`；
- DEBUG：`story_llm`（每次调用的角色与字数）、`environment_injected` 的 `story_lines`（状态卡注入了几行）。

**她在聊天里不提自己的生活：** 状态卡只是背景，不要求她主动说。确认 `environment_injected` 的 `story_lines > 0`；为 0 说明故事没开、状态卡已过期超过 `card_stale_hours`，或关了 `interaction.environment`。

**故事事件在哪：** 「概览」的事件列表里带「故事」标记的就是。它们可以像聊天事件一样 `/forget`、重建；重建时用的是故事专用的整理提示词。

## 备份与恢复

- WebUI 故事页签「导出故事数据」：世界种子、设定集、主持人笔记、全部场景转录（含剧透）；目前只能导出，不能整体导回；
- `/memory export full`：人格、活跃事实、事件；不含媒体、insights、excerpts；
- `/memory export seed`：小规模初始人格；
- `/memory restore <文件> [force]`：恢复 full；
- `/memory import <文件> [force]`：导入 seed；
- 非空库必须 `force`，恢复前自动备份 SQLite 和人格快照。

## 命令与工具参数

### 命令

| 命令 | 说明 |
|---|---|
| `/memory` | 状态 |
| `/memory export [seed\|full]` | 导出 |
| `/memory restore <文件> [force]` | 恢复 |
| `/memory import <文件> [force]` | 导入 seed |
| `/memory distill` / `consolidate` / `decay` | 手动整理 |
| `/forget <event_id>` | 删除事件 |
| `/persona rollback <version>` | 回滚人格 |
| `/qzone <正文>` | 发说说 |
| `/story` 或 `/story status` | 故事状态 |
| `/story import <文件>` | 导入世界种子（文件放 `seed/` 目录） |
| `/story on` / `off` | 开启 / 关闭当前机器人的故事 |
| `/story pause` / `resume` | 暂停 / 继续 |
| `/story wake` | 立刻演一场（不消耗额度） |

### 工具

| 工具 | 参数 |
|---|---|
| `say(text, session?)` | 文字；每轮最多 3 段 |
| `speak(text, session?)` | 语音；每轮最多 1 条、≤60 字 |
| `silent()` | 本轮不说话 |
| `notes(action, kind?, title?, content?, tags?)` | 私人笔记 |
| `memo(action, note?, at?, memo_id?)` | 备忘 |
| `media(action, ...)` | 图片库 save/list/search/delete |
| `send_image(source, caption?, session?)` | 发图 |
| `qzone(action, ...)` | 空间 |
| `memory_search(query)` | 显式查事实 |

## 测试

```powershell
$env:PYTHONPATH=".pydeps"
.venv\Scripts\python.exe -X utf8 -m pytest astrbot_plugin_shadow_memory/tests -q
```

- `tests/unit/`：模块与纯逻辑；
- `tests/integration/`：整条记忆链路；`test_story_main_wiring.py` 用 AstrBot 替身把 `main.py` 真正导入并跑一遍故事的接线；
- `tests/contract/`：AstrBot 接口与工具 schema；
- `tests/live/`：可选的真实模型冒烟。`smoke_story.py` 用真实模型连演几场故事并打印经过、骰子、状态卡和调用统计，只读环境变量 `DSH_TEST_TEXT_BASE_URL` / `DSH_TEST_TEXT_API_KEY` / `DSH_TEST_TEXT_MODEL`，数据写在临时库里；`--narration second|first|none` 换叙述人称，`--start 2026-11-23T08:00` 指定起始时间。

## 已知取舍

- 人格是上下文层工程，不是模型权重训练；
- 备忘录取上下文走工具返回；
- 图片描述优先主模型，失败才用视觉模型/兜底文案；
- 图片库按 bot 分目录；
- 官方 QQ 语音转换失败不会自动发文字；
- 群历史工具会增加 schema 和潜在 token，按需关闭。
