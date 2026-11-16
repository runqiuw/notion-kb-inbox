# Wiki 层：在知识库之上维护主题条目

知识库存的是一份份原始材料和零散笔记，问题一多就得反复翻。Wiki 层把它们综合成**主题条目**，
每条回答一个反复会问的问题；chatbot 查询时先看 Wiki 的总索引，再按需回到原始材料。

```
知识库收件箱（模板）          ← 复制一次就得到两层
├─ 🧠 知识库   Telegram bot 写入；全部资料 + 全部笔记 + 「知识库：聊天提炼」skill
└─ 📖 Wiki     chatbot 按 skill 维护；总索引 + Wiki 条目 + 维护记录 + 两个 skill
```

谁写什么：

| 层 | 谁写 | 写什么 |
|---|---|---|
| 知识库 | Telegram bot、[聊天提炼 skill](chat-capture-skill.md) | 原始材料、随想、阅读笔记、聊天提炼 |
| Wiki | chatbot（按维护 skill，**只在你要求时**） | 主题条目、总索引、维护记录 |

Wiki 只在 Wiki 一侧关联知识库（条目的「关联资料」「关联笔记」），**从不修改知识库里的页面**。
bot 不读也不写 Wiki，所以不用 Wiki 也完全不影响部署。

## 组成

- **Wiki 页（总索引）**：正文按主题列出每个条目的编号、链接、能回答的问题、检索别名，以及覆盖状态。
  chatbot 只读这一页就能判断有没有相关内容，无关就停，不去翻库。
- **Wiki 条目**：标题、编号（WIKI-001 起）、一句话说明、内容状态（草稿/可用/待修订/已归档）、
  核查状态、最近核查、关联资料、关联笔记、相关 Wiki、合并后指向。正文按问题 → 当前理解 →
  分歧与限制 → 尚未解决 → 来源与读取范围组织，每条结论标出来源代号。
- **维护记录**：每次维护一条，记下范围、改动、来源 → 条目映射、未完成项，供下次增量接续。
- **两个 Notion skill**：
  - 「Wiki：查询」：先读总索引 → 无关即停 → 相关再读 1–3 条 → 需要原文才回源。只读。
    Wiki 还没整理（只有示例或未做全量核对）时，会在知识库两个库里定向搜一次，并注明“尚未整理进 Wiki”，
    所以刚部署、还没建 Wiki 时也能查到知识库里的东西。
  - 「Wiki：维护与覆盖核对」：三种模式——“把这几页补进 Wiki”“维护一下 Wiki（增量）”“全面核对覆盖”。

两份 skill 的纯文本备份在 [`wiki-skills/`](../wiki-skills/)，可放进不读 Notion skill 的客户端。

## 安装

1. **复制模板**（[setup-notion.md](setup-notion.md) 3A）。复制后 Wiki 里的链接、条目关联、skill 里的数据库链接
   都会自动指向你的副本，两个 skill 页也仍是 Notion Skill；这一点已实际复制验证过。
2. **接入 chatbot**（任选）：
   - ChatGPT / claude.ai：在自定义指令或 Project 指令里加一句
     「问题涉及我积累的资料时，先用 Notion 读〈你的「Wiki：查询」页链接〉并按其流程处理」。
     只占几十字，skill 全文只在相关问题上读一次。
   - 支持 Notion Skill 的客户端（Notion AI、Notion MCP）：在 Notion 的 Library → Skills 里启用这两个 skill。
     是否会自动触发取决于客户端，作者主要靠上面的固定指令。
   - Claude Code / Codex：把 `wiki-skills/*.md` 复制进 skills 目录，并把文中「」名称换成你的 Notion 链接。
3. 删掉两条示例条目和示例维护记录，然后对 chatbot 说「全面核对 Wiki 覆盖」做第一次建库。

用 `tools/setup_notion.py --wiki` 建库的话，脚本会一并建好 Wiki 页和两个数据库，但不会创建 skill 页；
把 `wiki-skills/` 里两份内容贴进 Notion 新页面并标为 Skill，或直接用固定指令指向它们。

## 备份

Wiki 不在 bot 的写入范围内，但可以一起备份：把「Wiki 条目」「维护记录」的 database ID 填进
`NOTION_WIKI_DB`、`NOTION_WIKI_LOG_DB`（见 [backup.md](backup.md)）。

## 注意

- 维护会直接写 Notion，通常**不弹确认**。skill 规定只有你明确要求时才维护；普通查询只读。
- 条目是综合与导读，不是原文。问原话、具体数字时 skill 会要求回到来源页。
- 建议开着 Notion 的页面历史：维护写错时用被改页面自己的 Version history 回退。
