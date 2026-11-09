# 影忆 ShadowMemory（AstrBot 插件）

作者：绿荫（LvYin3）

影忆（ShadowMemory）是一个把「记忆」做成学习、把「出现」做成选择的 AstrBot 插件：

- **记忆模块**：话题事件、S/R 遗忘曲线、关联边、夜间巩固、事实有效期、人格蒸馏、`/memory` 审计命令。
- **交互模块**：群聊门控、`say / speak / silent` 分段回复、最终文本拦截、TTS、笔记、备忘录、会话目录。
- **QQ 空间**：发说说（仅第三方框架 aiocqhttp + `send_qzone_msg` 后端，官方框架自动隐藏）。
- **seed skill**：`skills/persona-seed/`，给外部 LLM 用，把用户口述的过去变成可导入的初始人格记忆。

目标版本：**AstrBot ≥ 4.29.3**（在 4.29.3 上核对过全部框架接口）。

---

## 安装

1. 把整个 `astrbot_plugin_shadow_memory/` 目录拷到 AstrBot 的 `data/plugins/` 下；
2. 重启 AstrBot（或在 WebUI 插件页启用）；
3. 打开插件配置填写 `decision`（判断模型）与 `text`（写作用模型）。

插件不新增第三方依赖：运行期用 AstrBot 自带的 `httpx`，存储用标准库 `sqlite3`。

数据目录：`data/plugin_data/shadow_memory/`

```
memory.db        SQLite（WAL）
seed/            persona_seed JSON（/memory import 从这里读）
exports/         /memory export 的输出
qzone_media/
  <bot_id>/      图片库（按机器人隔离；发说说、发图片都从这里取）
```

## 配置要点

### 判断模型（System One）

**方式 A（推荐）：直接选 AstrBot 的 System One provider**

AstrBot 4.29.3 自带「判断 / System One」类型的 provider：

1. 模型提供商 → 新增 → 类型选「判断」；
2. 填你自己的 endpoint、key 和模型 ID（端点需支持 System One）；
3. 插件配置里 `decision.provider_id` **手动填写**这个 provider 的 ID。AstrBot 的提供商下拉只列对话模型，判断 provider 没有对应的 `_special`，所以不能从下拉选。

这样插件不再需要 `base_url / api_key`；超时与重试由 AstrBot 的 provider 自己控制。插件会检查它确实有 `ask`（不是误选的对话 provider）。

**方式 B：插件直连兜底**

如果你有一个兼容 System One 的 OpenAI 风格端点：

```jsonc
"decision": {
  "provider_id": "",                                  // 留空才走直连
  "base_url": "https://your-endpoint.example/v1",     // 换成你的端点
  "api_key": "...",
  "model": "your-decision-model",
  "protocol": "auto",                                 // auto | systemone | chat
  "timeout_ms": 3000
}
```

- `auto`：先试 `{base_url}/systemone`，404/405/响应不合法就退回 `/chat/completions`，并在本轮进程内记住结果。
- `systemone` 原生接口会返回 `noul` 概率、`choice` 概率分布，`score` 是 **0 基**（0..N-1）。
- `timeout_ms` 建议从 3000 起，按端点实际延迟调整。
- **优先级**：provider 可用 → provider；provider 缺失/不是判断类型/调用失败 → 直连（若配了 `base_url` 和 `model`）；两者都没有 → 静默。
- 失败语义：判断失败 → 该轮不注入 / 不回复；连续 3 次失败 → 10 分钟退避。

### 写作用模型

优先用 AstrBot 已配置的对话模型（`text.provider_id`，留空用会话默认）；
只有在 `text.base_url` 填了直连地址时才会走插件自己的 OpenAI 兼容客户端。
碎片、gist、摘要、蒸馏、事实抽取都会走它。

### 思考模式与强度

插件内每个写作调用都带 `task` 标签，按用途决定思考模式（**直连 `text.base_url` 时生效**）：

| 用途 | 默认 | 理由 |
|---|---|---|
| `fragment` 话题关闭碎片 | disabled | 短结构化输出，省延迟与 token |
| `tension` 冲突张力草稿 | disabled | 一句话，在 post_turn 里跑 |
| `facts` 夜间事实抽取 | disabled | 批处理，结构化 |
| `distill` 人格蒸馏 | enabled | 质量优先，周级离线 |
| `consolidate` 夜间巩固 | enabled | 质量优先，夜间离线 |
| 图片描述 | disabled | 见 `vision.disable_thinking` |

配置：

```jsonc
"text": {
  "thinking": "auto",            // auto 不传 / enabled / disabled
  "reasoning_effort": "",        // "" 用端点默认 / low / high / max
  "thinking_by_task": {          // 按用途覆盖；空字符串跟随 thinking
    "fragment": "disabled",
    "tension": "disabled",
    "facts": "disabled",
    "distill": "enabled",
    "consolidate": "enabled"
  }
}
```

DeepSeek 事实（[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)）：

- 思考模式**默认开启**，默认 effort 为 `high`；关掉用 `{"thinking": {"type": "disabled"}}`；
- 思考模式下 `temperature` / `presence_penalty` / `frequency_penalty` 不生效；
- 端点不认识这些字段时，插件会自动去掉重试，不影响生成。

**provider 路径的限制**：AstrBot 的 `text_chat(**kwargs)` 不会把按调用参数合进请求（`openai_source.py:956-1006`），所以走 `text.provider_id` / 会话默认 provider 时，插件的 task 策略无法生效——只在 `stats.thinking_skipped` 里计数，并打一条 INFO（不再 WARN）。此时只能在 provider 的 `custom_extra_body` 里全局配：

```json
{"thinking": {"type": "disabled"}, "reasoning_effort": "low"}
```

这会同时影响主对话；想按用途区分就用直连。主对话的思考由 AstrBot provider 决定，判断模型没有思考模式。

### 固定聊天上下文

只要插件参与一次 LLM 请求，就会把该会话最近 N 条消息（用户 + 机器人回复）补进请求：

- `context.recent_messages` 默认 20（0 关闭），`context.max_chars` 默认 2000；WebUI「设置」里可按 bot 覆盖，0 = 关闭。
- `req.contexts` 非空时只补缺失消息，不截断已有历史；为空时用临时段注入。
- memo 唤醒时 `memo get` 的返回也会带最近 N 条。
- AstrBot 自带 `provider_ltm_settings.group_icl_enable`（或 `active_reply.enable`）开启时，插件跳过补全并在 WebUI 提示，避免重复。
- 记录表 `session_messages` 每个 `(bot, umo)` 保留最近 200 条。

### 交流环境与工具提示词

默认注入一段**第一人称、拟人**的环境块，告诉模型“我在 QQ 上、在和谁说话、要用工具说”：

```markdown
# 我这会儿
- 我在 QQ（aiocqhttp）上，正跟小明聊天。
- 这会儿在群聊「摸鱼群」里。

- 我说文字消息必须调用 say 函数（say(text)）；不调用 say，我写的话就发不出去。
- 比如对方说“在吗”，我就调 say(text="在吗")，不写正文。
```

只说明环境与 `say`，不规定群里该不该插话、用什么风格；自定义 `environment.prompt` 只替换最后那条规则，动态场景仍自动拼。

配置：

```jsonc
"interaction": {
  "environment": {
    "enabled": true,
    "prompt": ""     // 留空用内置第一人称规则；填了只替换规则段，动态信息仍自动拼
  },
  "say_example": true  // 在上下文末尾注入一条临时 say 工具调用示例
}
```

- **纯文本 assistant 先例**（人格开场对话、上一轮被丢弃的回复）会让模型继续回正文。默认在 `req.contexts` 末尾注入一对临时 `assistant tool_calls(say)` + `tool` 结果，不写回会话历史；`say_example=false` 可关闭。
- `drop_final_text` 里的 `say_example` 表示本轮是否注入了该示例，`say_example_injected` 是 DEBUG 日志。

- 默认不发送：没调 `say/speak` 时最终文本被丢弃（严格静默）。
- 需要保底时打开 `fallback_send_final_text=true`（强制输出）：没调工具也把最终文本作为一条普通消息发出。
- 不再根据“本轮有没有工具”自动发送。
- `bot_overrides` 支持 `environment_enabled` / `environment_prompt` 按 bot 覆盖。
- 群聊/私聊、群名、对方昵称自动切换；不重复 AstrBot 已有的用户 ID/时间。
- memo 唤醒的 `memo get` 也带一段精简版第一人称环境。
- 每次成功注入会打一条 INFO：`environment_injected {"bot_id": ..., "chat_type": ..., "chars": ..., "preview": ...}`；没有这条日志时检查 `interaction.enabled` / `interaction.environment.enabled` 或看 `交流环境注入失败` 告警。
- `say / speak / silent` 的 docstring 同步改成第一人称描述。
- 先例修复原理：直连 `deepseek-flash` 时，只要 messages 里出现过一条 assistant 纯文本，它就不再返回 tool_calls；把最后一条 assistant 换成 `say` 的 tool_call 示例后恢复。因此 `say_example` 默认开启。

### 记忆归属

召回内容是**第一人称原文**，如果直接塞进当前 user 消息，模型会把“我”读成对方。现在固定做三件事：

1. 每次请求都把固定说明追加到 `system_prompt`（人格之后、环境之前），内容不随召回变化，system 前缀缓存稳定：

   ```markdown
   # 关于我的记忆
   - 【我的记忆，不是对方说的】是我自己亲历或想起的事；里面的“我”是我。
   - 【我知道的事，不是对方刚说的】是我知道的信息，不是刚刚听对方说的。
   - 这些标签只是给我自己看的，不会出现在我说的话里。
   ```

2. 召回块合成为一个临时 `TextPart`，前面空两行，不落历史：
   - 事件/张力：`【我的记忆，不是对方说的】` + `我想起：…` + `我心里还留着：…`；
   - 事实（`fact_ids` 非空）：`【我知道的事，不是对方刚说的】` + `我知道：…`。
3. `recall_inject` DEBUG 日志记录 `kind=self|fact`、段数、字数、标签；需打开 `logging.debug`。

实验依据：同一条“我爸妈支持我喜欢的东西”，放 system prompt 时模型归给自己；放 user 消息无标签时会被当成对方说的；带标签后能区分。把可变回忆直接搬进 system prompt 归属更强，但每次召回都会破坏 system 缓存，所以采用“固定说明 + 带标签的临时 user 块”。

### 工具调用不生效（模型只回正文）

AstrBot 在请求 provider 前有个过滤：如果对话模型的 `modalities` 是非空列表且**不含 `tool_use`**，会把整个 `tools` 清空（`tool_loop_agent_runner._func_tool_for_provider`）。插件 hook 里能看到 `tools:13`，但模型实际收到 0 个工具。

插件会打 WARN：

```
[mem] provider_tool_use_disabled bot=... model=deepseek-flash modalities=['text']；
AstrBot 会在请求前清空 tools，请在对话模型的「模型能力」里勾上 tool_use
```

处理：WebUI → 服务提供商 → 该对话模型 → 「模型能力」→ 勾选 **工具调用 / tool_use**，保存后重发。之后 `llm_response` 应该从 `"tools": []` 变成 `"tools": ["say"]`。

### 人格注入调试

在插件配置「日志与审计 → Debug 日志开关」打开（`logging.debug`，默认关），并把 AstrBot 日志级别调到 DEBUG 后，每次 LLM 请求会看到 `[mem]` 的 `persona_inject_*`：

```jsonc
"logging": {
  "debug": true   // 默认 false；只控制 DEBUG 诊断，普通 INFO 日志不受影响
}
```

- `persona_inject_start`：bot_id、umo、`memory_enabled`；
- `persona_inject_prepared`：人格字数、回忆段数、`reason`、`veto`、`persona.mode`、注入前 system_prompt 长度；
- `persona_inject_applied`：`plugin_only` 的 base_card 字数、人格字数、system_prompt 前后长度；
- `persona_inject_skip`：`reason=memory_disabled|empty_query|empty_persona`；
- `persona_inject_done`：回忆段数、最终 system_prompt 长度；
- `persona_inject_error`：异常原因。

人格注入失败不会抛到聊天，顶层只打 WARN；DEBUG 日志能区分是“没开记忆 / 没人格 / prepare_turn 异常 / 正常但内容为空”。

### TTS（按机器人）

- 解析顺序：插件 DB `kv.tts_provider_id` → `interaction.tts_provider_ids[bot]` → 无；**不再回退 AstrBot 会话默认**。
- 未显式绑定 = 不合成语音，`speak` 内容按文字发出；已绑定但合成失败也回退文字。
- WebUI「设置」里每 bot 一个下拉，列出 AstrBot 全部 TTS provider；「静音」写空字符串，「跟随配置」删 kv。

### 人格来源

- `persona.mode = append`（默认）：AstrBot 的 persona 作基底，插件只追加以记忆蒸出来的人格块；
- `persona.mode = plugin_only`：把 AstrBot persona 留空，插件用 `base_card` + 人格块。

想按机器人分别设置，用 `bot_overrides`：

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

## WebUI（记忆总览）

插件详情页 →「记忆总览」视图（`views/memory/`）：

- **概览**：已启用的平台适配器（即使还没记忆也显示 0）/ 有数据 / 仅配置 / kv 里出现过的 bot；人格三层、事实、事件、洞察、笔记、图片、最后活动、TTS 与上下文来源。
- **明细**：六类各 20 条只读。
- **转移**：复制（源保留），默认合并去重，可选覆盖（输入目标 bot_id）；六类可勾选；覆盖前整库备份 + 人格快照，目标媒体移到 `exports/trash_*`；媒体上限 100MB / 500 文件，绕过图库配额。
- **导入/导出**：full v2（人格/事实/事件/洞察/笔记，不含媒体）与 seed（≤20 事件 / ≤50 事实）；导入非空必须覆盖，自动备份，10MB 上限。
- **设置**：每 bot 的 TTS 与固定上下文 N。

> 视图接口返回「未找到该路由」时：改完 `main.py` / 新增视图后必须**重载插件**（插件管理 → 重载，或禁用再启用），只刷新页面只会重新加载静态资源。仍失败时看日志里的 `webui_routes` / `注册 WebUI 路由失败`，并确认 AstrBot ≥ 4.29.3。

## 命令（管理员 + 仅私聊）

| 命令 | 作用 |
|---|---|
| `/memory` | 当前机器人的记忆状态（人格/S·R/边/事实/注入/门控/缓存） |
| `/memory export [seed\|full]` | 导出到 `exports/` |
| `/memory restore <文件> [force]` | 从 full 备份恢复；`force` 会先备份整个数据库 |
| `/memory import <文件> [force]` | 从 `seed/<文件>.json` 导入初始人格记忆 |
| `/memory distill` / `consolidate` / `decay` | 手动触发一次 |
| `/forget <event_id>` | 删一条事件（级联删边；相关人格条目失去来源则停用） |
| `/persona rollback <version>` | 回滚人格版本 |
| `/qzone <正文>` | 手动发一条说说（绕过每日软上限） |

### 备份与恢复

- `/memory export full` 产出 `shadow_memory/full/v1`：人格、活跃事实（≤500）、事件（≤500，含 salience / storage_strength / retrievability / status）。
- 把 JSON 放到 `data/plugin_data/shadow_memory/exports/`（或 `seed/`）后：
  - 空库：`/memory restore xxx_full.json`
  - 已有记忆：`/memory restore xxx_full.json force`
- `force` 覆盖前会自动把整个 SQLite 备份到 `exports/backup_<bot>_<ts>.db`，并留一份人格快照。
- 恢复会重映射事件 id，并重建 `facts.source_event_id` / 人格来源引用；边按实体重新连。
- **不恢复**：insights（full 导出本身不含）、事件轮次原文 / excerpts、媒体库、日志、人格历史版本。

## 工具（模型可调用）

| 工具 | 说明 |
|---|---|
| `say(text, session?)` | 说一句；每轮最多 3 段 |
| `speak(text, session?)` | 用声音说一句；每轮最多 1 条、≤60 字；TTS 失败自动转文字 |
| `silent()` | 这次不说话 |
| `notes(action, …)` | 她自己的本子（笔记/日记），永不进 prompt |
| `memo(action, …)` | 给自己设提醒；到点唤醒后先 `get` 取实时上下文 |
| `media(action, …)` | 图片库：`save / list / search / delete`；`save` 时可写描述，留空则插件自动看图补一句 |
| `send_image(source, caption?, session?)` | 发一张图：`event:1` / 图片库文件名 / URL；可带一句配文；每轮独立上限 |
| `qzone(action, …)` | 发说说（`images` 保留为 `media list` 的兼容别名） |
| `memory_search(query)` | 显查事实（数据口吻，进历史） |

## 图片库与发图

- **存图**：`media(save, source="event:1", description="他说是海边拍的", tags=["海边"])`，
  或 `source="https://..."`。来源只有这两种：不读本地任意路径、不接受模型直接塞 base64。
- **查图**：`media(search, query="海边")` 会按描述、标签、文件名、发送人做模糊匹配，
  返回「文件名｜描述｜大小」，再拿文件名去 `send_image`。
- **命名**：`[前缀-]YYYYMMDD-HHMMSS-<hash6>.<真实扩展名>`；扩展名按文件头嗅探，不信 URL。
- **配额**：每机器人默认 100 张 / 200MB；超限拒绝并提示先删（`media(delete, filename=...)`）。
- **发图**：图片不占 3 段文字预算，走 `interaction.send_image.max_per_turn`（默认 2 张/轮）；
  `caption` 不占文字段，但会计入当轮“我说过的话”。
- **URL 安全**：只允许公网 http(s)，每跳重定向都重新校验；内网/回环/保留地址直接拒绝。
- **目录隔离**：图片落在 `qzone_media/<bot_id>/`，机器人之间互不可见。

### 让机器人看见图

- AstrBot 只有在对话 provider 的「模型能力」里勾了**图像**（`modalities` 含 `image`）时，才会把用户发的图交给主模型；没勾时图会被替换成“未包含在本次请求里”的通知。
- 推荐模型：`deepseek-flash`（DeepSeek 官方视觉模型，支持 base64 data URL / 公网 URL / Files API，格式 JPEG/PNG/GIF/WebP，单图约 ≤1024 tokens）。
- 主模型是纯文本时，有两条路：
  1. 配 AstrBot 的 `default_image_caption_provider_id`，核心会把描述以 `<image_caption>` 注入请求；
  2. 交给插件兜底：`vision.enabled=true`，`vision.provider_id` 指向视觉 provider（留空则用 AstrBot 打标 provider，再空用写作 provider），直连时 `vision.model=deepseek-flash`。
- 分层顺序：**主模型看得到 → 它自己写 `description`；看不到 → 插件调视觉模型补一句；视觉也失败 → 兜底文案**（“对话里的一张图”/“链接存下的图”）。
- 视觉调用只在 `description` 为空时发生，默认 20s 超时、描述 ≤80 字；失败不阻断存图。
- `deepseek-flash` 是思考模型：直连时默认带 `thinking: {"type": "disabled"}`（实测把一次打标从 ~2.8s/333 推理 token 压到 ~0.9s/7 输出 token）；端点不认识这个字段会自动去掉重试；输出预算默认 800，`finish_reason=length` 且内容为空时自动翻倍再试一次。

## 行为要点

- **群聊门控**：非 @ 群消息逐条问判断模型 `reply_now`（阈值 0.50），通过才把 `event.is_at_or_wake_command` 置真让原生 LLM 链路跑；不通过就是原生沉默。没有冷却、没有每小时上限。
- **最终文本**：本轮只要 `say/speak/send_image` 发过，`on_decorating_result` 就清空 `result.chain`，最终文本不发。没调任何发送工具时默认**不发送**（严格静默）；只有 `fallback_send_final_text=true`（强制输出）才把最终文本作为一条普通消息发出。
- **回忆注入**：`req.extra_user_content_parts + mark_as_temp()`，不落历史；召回块带“我的记忆/我知道的事”归属标签；固定归属说明进 `req.system_prompt`，人格块按版本缓存。
- **备忘录取数**：AstrBot 的 cron 唤醒路径不触发 `on_llm_request`，所以唤醒轮靠 `memo(action="get")` 现场取「提醒 + 现在 + 人格 + 会话目录 + 最近对话 + 第一人称环境」。
- **QQ 空间**：启动时/每次平台加载后检查是否存在 aiocqhttp 实例；没有就 `deactivate_llm_tool_async("qzone")`，有就重新激活。发布走：

  ```python
  await event.bot.call_action("send_qzone_msg", content=..., images=[...], ugc_right=1)
  ```

  图片只接受 http(s) 链接、当前机器人图片库里的文件名、或 `event:<n>` 指代当前消息里的第 n 张图；`ugc_right` 仅支持 1/4/64（16/128 需要目标 QQ 号，未开放）。

## 测试

```powershell
# 工作区根目录
$env:PYTHONPATH=".pydeps"          # 仅本机脚手架的 pytest
.venv\Scripts\python.exe -X utf8 -m pytest astrbot_plugin_shadow_memory/tests -q
```

- `tests/unit/`：动力学、边、分段、人格、召回、记忆归属、审计日志、蒸馏、巩固、事实、衰减、seed、决策协议、判断 provider 适配与路由、固定上下文、交流环境、会话缓冲、TTS 绑定、转移、WebUI、发送、门控、备忘录、笔记、图片库、发图、视觉描述、QQ 空间、迁移；
- `tests/integration/`：整条记忆链路、双机器人隔离、门控→分段、空间发布、seed 后继续生活；
- `tests/contract/`：直接读 AstrBot 源码断言接口仍在（`ASTRBOT_SRC` 可覆盖路径），并用 `docstring_parser` 校验所有 LLM 工具的 schema；
- `tests/live/smoke_models.py`：手动真实模型冒烟（读工作区 `.env`，不打印密钥）。

在普通机器上直接 `pip install pytest pytest-asyncio docstring-parser` 即可，不需要 `.pydeps`。

## 已知取舍

1. 备忘录取上下文走工具返回，而不是系统段（cron 路径不触发 `on_llm_request`）。
2. 会话目录的「最后一条消息摘要」是 40 字截断，不额外调模型。
3. 显查通道用新增的 `memory_search` 工具实现。
4. `fallback_send_final_text` 默认关闭：不调工具 = 不发送；打开才是强制输出。没有“无工具自动发送”的特殊路径。
5. 事实抽取在夜间批处理，不占每轮预算。
6. `plugin_only` 不剥离 AstrBot persona，只追加自己的卡片。
7. 注入率 15–20%、缓存命中 ≥80% 需要真实流量标定，本地只验公式与合成数据。
8. 图片描述优先由主模型写（需要 provider 勾选「图像」）；主模型看不到时插件用视觉模型兜底；视觉也失败才用兜底文案。不做缩略图 / 宽高解析。
9. 图片库按机器人分目录；`qzone_media/` 根下的旧平铺文件不再可见，需要时手动移进 `<bot_id>/`。
10. 走 AstrBot provider 时无法按用途区分思考强度（框架丢弃按调用参数）；只有直连 `text.base_url` 才能按 task 控制，否则用 provider 的 `custom_extra_body` 全局配。
11. 走 AstrBot 判断 provider 时，超时与重试由 provider 自己控制；插件的 `decision.timeout_ms / protocol` 只作用于直连兜底。
12. 固定上下文只补 `session_messages` 里的最近消息；fallback 直发的最终文本、图片描述暂不写入缓冲。AstrBot 自带群聊上下文开启时插件让位。
13. full v2 不含媒体文件；图片迁移用 WebUI「转移」。seed 导出保证可再导入，因此会截到 20 事件 / 50 事实。
14. 媒体转移绕过目标图库 100 文件 / 200MB 配额，只受 100MB / 500 文件硬上限；完成后概览会标红。
15. 交流环境提示词默认第一人称；不自动拼用户 ID/时间（AstrBot 已有对应开关），只拼平台、群聊/私聊、群名、昵称。

## 安全

- 密钥只写在 AstrBot 插件配置或本机 `.env`；日志只记消息长度与前 40 字，密钥永不入日志。
- 管理命令仅管理员 + 私聊 + 当前机器人。
- `seed` 导入默认拒绝非空库；`force` 会先留人格快照。
- 发说说只允许白名单目录内的文件，拒绝路径穿越与超限图片。
