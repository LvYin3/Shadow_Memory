# persona_seed v1 · 字段与校验

插件的导入器会**整文件校验**：任何一条不合规，整份文件都会被拒绝，并逐条返回原因。
所以交稿前请按本页自查。

## 顶层

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `version` | string | 建议 | 固定 `persona_seed/v1`（也接受以 `persona_seed` 开头） |
| `bot_hint` | string | 否 | 给哪个机器人用，仅备注 |
| `persona` | object | 是 | 三层人格 |
| `facts` | array | 否 | 稳定事实，最多 50 条 |
| `events` | array | 否 | 情景记忆，最多 20 条（建议 3–5 条） |

## persona

| 层 | 条数上限 | 每条字数 | 写什么 |
|---|---|---|---|
| `core` | 6 | 50–70 | 价值观、默认反应、自我认知 |
| `edge` | 7 | 50–70 | 具体场合下的偏好、试探性判断 |
| `tension` | 3 | 50–70 | 还悬着、没想明白的事 |

- 全部第一人称，写"带条件的倾向"。
- 校验容忍区间：导入时允许 40–90 字（配置 `entry_min_chars` / `entry_max_chars` 决定），
  但请按 50–70 写，超出容易在夜间蒸馏里被判不合格。

## facts[]

| 字段 | 类型 | 说明 |
|---|---|---|
| `kind` | string | `profile` / `preference` / `commitment` / `world`，非法值按 `world` |
| `subject_id` | string | 可选，挂在谁身上（对方用户 id） |
| `text` | string | 1–120 字，独立成立的一句话 |
| `key_fact_type` | string | `none` / `time` / `address` / `amount` / `promise`；后四种会成为关键事实（不衰减） |
| `salience` | number | 1–5，默认 3 |

## events[]

| 字段 | 类型 | 说明 |
|---|---|---|
| `fragment` | string | 1–60 字，第一人称片段 |
| `summary` | string | ≤300 字，可空 |
| `entities` | string[] | 3–8 个具体名词（人名/地点/物品/话题/作品名），会自动小写去重 |
| `occurred_days_ago` | number | 距今多少天，0–3650 |
| `salience` | number | 1–5，默认 3；决定初始存储强度 S0 = 7 × m(salience) |
| `participants` | array | 可选，`[{"id":"u1","name":"小明"}]` |

导入时事件的 S/R：`S = 7 × m(salience)`、`R = 1`、`last_recalled_at = 导入时刻`；
事实：`S = 30 × m(salience)`（关键事实 `S = 9999` 且不衰减）。

## 常见拒绝原因

- `persona.core[0] 长度 32 不在 40-90`
- `persona.core 超过上限 9`
- `facts[2] 文本为空或超过 120 字`
- `events[0] 碎片为空或超过 60 字`
- `当前机器人已有记忆，拒绝导入；确认覆盖请加 force`

## 导入语义

- 空库：直接写入；
- 非空库：默认拒绝；加 `force` 会先写一份人格快照，再清空该机器人的
  情景/事实/洞察/人格三层后导入（笔记与备忘录不受影响）；
- 导入只作用于当前机器人（`bot_id`），不跨机器人。
