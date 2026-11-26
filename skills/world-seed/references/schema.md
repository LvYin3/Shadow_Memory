# world_seed v1

插件导入时整份校验。有一条不合规，整份都不写入。

| 字段 | 约束 |
|---|---|
| `version` | 以 `world_seed` 开头，写作 `world_seed/v1` |
| `title` | 可空，不超过 40 字 |
| `outline` | 必填，50 到 3000 字（给背景模型看） |
| `known_outline` | 可选，不超过 1000 字。第一人称角色已知的世界常识底稿（不含秘密与导演元规则）；留空时导入自动提炼 |
| `entries` | 数组，最多 200 条。每条有 `category`、`name`（1–40 字）、`body`（1–500 字），以及可选的 `visibility`（`known` 或 `gm_only`，留空时导入自动分类，失败保守默认 `gm_only`）。名字不能重复 |
| `hooks` | 数组，最多 10 条，每条不超过 200 字 |
| `opening` | 对象，可空。`where` 不超过 40 字，`doing` 不超过 60 字 |

`category` 接受：`person` / `place` / `org` / `rule` / `item` / `other`，以及中文「人物 / 地点 / 组织 / 规则 / 物品 / 其他」。不认识的归到其他。

种子条目和演出中追加的设定不是同一层。导入会换掉种子条目，不改已经演出来的设定集。
