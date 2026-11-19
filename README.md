# 影忆 ShadowMemory

> 用记忆塑造人格的 AstrBot 插件。

作者：绿荫（LvYin3）  
目标版本：**AstrBot ≥ 4.29.3**

##### ⚠️ 使用前务必在 AstrBot 中调整「压缩前最多保留对话轮数」，不要使用默认的 `-1`。建议设为 `20`。

---

## 它是什么

影忆不是把聊天记录塞进提示词，而是把对话慢慢整理成角色的「过去」：

- 发生过的事 → 事件；
- 确定下来的信息 → 事实；
- 人和事的联系 → 关系边；
- 没想明白的冲突 → 未消解张力；
- 反复出现的模式 → 人格倾向。

这些记忆会通过召回、常驻背景、夜间巩固、人格蒸馏和遗忘曲线，逐渐影响角色怎么说话、怎么判断、什么时候沉默。

## 其他拟人化设计

- **第一人称记忆归属**：召回内容明确是“我的记忆”，不是“对方刚说的”；“我”和“小明”在入库时就区分。
- **选择性接话**：群聊非 @ 消息先由判断模型决定要不要接，而不是每条都回复。
- **分段回复与声音**：`say` 说文字、`speak` 发语音、`silent` 选择沉默。
- **遗忘与余波**：S/R 曲线让记忆自然降温；未消解的张力会留下一点情绪余波。
- **常驻背景**：最近的人、事和重要记忆进入 system prompt，只影响语气，不主动提起。
- **生活痕迹**：私人笔记、备忘提醒、图片库、QQ 空间，让角色不只会聊天。
- **夜间整理**：巩固重复事件、抽取事实、蒸馏人格，把短期经历变成长期倾向。

## 原理边界

**这套原理是理想化的工程近似，不是严格的认知科学模型，也不是在模拟真实意识。**

- 不训练模型权重；“人格”来自 persona、system prompt、记忆注入和蒸馏结果的组合。
- S/R 遗忘曲线、实体聚类、巩固、人格蒸馏都是启发式规则，不是对大脑的精确复刻。
- 模型可能忽略、误解或违背记忆；效果取决于模型、数据量、配置和平台。
- 记忆越多不一定越好：会增加 token、延迟和上下文噪音，需要配合召回阈值和遗忘机制。
- 不要把它当成“真正会记住一切的电子生命”；它更像一个会随着相处逐渐有偏向的角色层。

## 功能

- **记忆**：事件、事实、关系边、遗忘曲线、张力、人格三层、夜间巩固、人格蒸馏。
- **交互**：群聊门控、`say / speak / silent` 分段回复、最终文本拦截、TTS、固定上下文、第一人称环境。
- **生活**：笔记、备忘提醒、图片库、发图、QQ 空间。
- **运维**：WebUI 记忆总览、导入导出、转移、`/memory` 审计命令、人格回滚。
- **初始人格**：`skills/persona-seed/`，把口述过去整理成可导入的 seed。

## 快速开始

1. 把整个 `astrbot_plugin_shadow_memory/` 放到 AstrBot 的 `data/plugins/`；
2. 重启 AstrBot，或在 WebUI 插件页启用；
3. 配置判断模型、写作模型，并把 `max_turns` 改成 `20`。

数据目录：

```text
data/plugin_data/shadow_memory/
  memory.db       SQLite（WAL）
  seed/           persona_seed JSON
  exports/        导出与备份
  qzone_media/
    <bot_id>/     按机器人隔离的图片库
```

插件不新增第三方运行依赖：HTTP 用 AstrBot 自带的 `httpx`，存储用标准库 `sqlite3`。

## 最小配置

```jsonc
{
  "decision": {
    "provider_id": "你的判断 provider ID",
    "full_context": true,
    "full_context_max_chars": 16000
  },
  "text": {
    "provider_id": ""   // 留空用 AstrBot 会话默认对话模型
  }
}
```

- `decision` 用于群聊门控、召回选择、生成后判断和重要记忆判断；可以是 AstrBot 的判断 provider，也可以走插件直连兜底。
- `text` 用于碎片、摘要、事实抽取、巩固和人格蒸馏；留空时跟随会话默认模型。
- `full_context=true` 会把完整人格和文本历史发给外部 JEV 服务；不想发送隐私就关闭。
- AstrBot 的 `agent_runner.config.compression.max_turns` 默认可能是 `-1`，务必改成 `20`，否则历史会无限增长。

完整配置见 [docs/CONFIG.md](docs/CONFIG.md)。

## 常用命令

管理员 + 仅私聊：

| 命令 | 作用 |
|---|---|
| `/memory` | 当前机器人的记忆状态 |
| `/memory export [seed\|full]` | 导出记忆 |
| `/memory restore <文件> [force]` | 从 full 备份恢复 |
| `/memory import <文件> [force]` | 导入 seed |
| `/memory distill / consolidate / decay` | 手动触发整理 |
| `/forget <event_id>` | 删除一条事件 |
| `/persona rollback <version>` | 回滚人格版本 |
| `/qzone <正文>` | 手动发一条说说 |

## 工具

| 工具 | 作用 |
|---|---|
| `say` / `speak` / `silent` | 说文字 / 发语音 / 选择沉默 |
| `notes` / `memo` | 私人笔记 / 备忘提醒 |
| `media` / `send_image` | 图片库 / 发图 |
| `qzone` | 发 QQ 空间说说 |
| `memory_search` | 显式查事实 |

详细参数见 [docs/DEBUG.md](docs/DEBUG.md)。

## WebUI

插件详情页 →「记忆总览」：

- 概览与只读明细；
- 记忆转移、导入/导出；
- 按 bot 设置 TTS 与固定上下文；
- 重建/刷新事件；
- 常驻背景只读展示。

改完插件代码或新增视图后需要**重载插件**，只刷新页面不够。

## 已知限制与安全

1. **人格不是稳定实体**：模型可能忽略或违背记忆；蒸馏和巩固也可能失败。
2. **token 与延迟**：完整上下文、工具调用和夜间整理都会消耗 token；建议 `max_turns=20`，不需要群历史时关闭 `group_message_history_enable`。
3. **外部 JEV 隐私**：开启 `full_context` 后，人格和历史会发到外部判断服务。
4. **平台差异**：QQ 空间依赖 `aiocqhttp`/`napcat`；QQ 官方语音需经过 Tencent SILK；不同适配器的消息类型和发送能力不同。
5. **密钥安全**：API key 只写在 AstrBot 插件配置或 `.env`；日志只记录长度和截断预览，不记录密钥。
6. **不是真实记忆**：不要期待它像人类一样持续学习、永久记住一切或产生真实情感。

## 文档与测试

- [配置详解](docs/CONFIG.md)
- [调试与排障](docs/DEBUG.md)
- 测试：`tests/unit/`、`tests/integration/`、`tests/contract/`、`tests/live/`

```powershell
$env:PYTHONPATH=".pydeps"
.venv\Scripts\python.exe -X utf8 -m pytest astrbot_plugin_shadow_memory/tests -q
```
