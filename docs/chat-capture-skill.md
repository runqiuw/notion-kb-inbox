# 聊天提炼 skill：让 chatbot 直接写入知识库

除了把 ChatGPT / Gemini 分享链接发给 Telegram bot（见 [chat-shares.md](chat-shares.md)），
还可以在 ChatGPT 等 chatbot 里**直接**把当前聊天提炼进知识库。[`chatgpt-plugin/`](../chatgpt-plugin/)
就是作者自用的这个插件，已去掉个人配置。

在聊天里单独发一句 `@知识库聊天提炼`，chatbot 会：

- 读取整场相关讨论，区分你的观点、AI 的建议和尚未验证的说法；
- 在「全部笔记」查重，同一讨论再次调用时在原页追加补充，不新建、不覆盖手写内容；
- 按来源 URL / DOI / arXiv ID 关联「全部资料」里已有的条目；
- 折叠保留可获取的原始对话和来源，按原讨论时间排时间线，写完读回验收。

新建的笔记是 `类型=聊天提炼`、`样例来源=用户提供`、`笔记状态=待展开`，会出现在模板首页的「聊天提炼」栏。

## 和 Telegram bot 的关系

两者写同一个 Notion 知识库，但互不通知：bot 的 URL 去重索引在 Firestore，chatbot 写的笔记不会登记进去。
同一场聊天不要两边都存，否则会重复。

## 文件结构

```
chatgpt-plugin/
  plugin.json                  插件清单（agent-plugins 格式）
  .app.json                    声明依赖 ChatGPT 的 Notion app
  .codex-plugin/plugin.json    Codex 用的清单
  skills/notion-kb-chat-capture/
    SKILL.md                   主流程（末尾内联了下面两份参考，便于只读一个文件的宿主）
    references/extraction.md   提炼、观点归属与原文保留规则
    references/notion-space.md 你的知识库位置配置 ← 需要改
    agents/openai.yaml         显示名称和默认提示词
```

## 配置：填你自己的 ID

`references/notion-space.md` 和 `SKILL.md` 末尾「知识库配置与入口兼容」一节里各有一张同样的表，
把所有 `<...>` 换成你的值（两处保持一致）：

| 占位符 | 取法 |
|---|---|
| 知识库首页链接 | 复制模板后首页的链接 |
| 全部笔记 / 全部资料 database ID | 以整页打开数据库，链接里 `?v=` 前面那 32 位（同 [setup-notion.md](setup-notion.md) 3A） |
| 全部笔记 / 全部资料 data source ID | 在已连接 Notion 的 chatbot 里让它 fetch 该数据库链接，返回里的 `collection://...` 就是；也可以在数据库菜单的数据源设置里复制 |
| 全部标签页链接、聊天提炼视图（可选） | 不填也能用，只是少一份标签说明和一次首页显示核对 |

字段名（标题、类型、样例来源、笔记状态、主题、关联资料）与 [notion-schema.md](notion-schema.md) 一致，
用模板或 `tools/setup_notion.py` 建的库都能直接用；改过字段名的话 skill 里也要同步改。

## 安装

- **ChatGPT**：在 `chatgpt-plugin/` 目录里打包成 zip（`plugin.json` 在 zip 根目录），
  按 ChatGPT 当前的插件导入入口上传，并确认已连接 Notion app、授权了你的知识库页面。
- **Claude Code / Codex 等支持 skill 目录的 agent**：把 `skills/notion-kb-chat-capture/` 复制到
  `~/.claude/skills/` 或 `~/.codex/skills/`，并确保该端已接入 Notion 连接器。
- **其他 chatbot**：把 `SKILL.md` 全文放进项目指令或自定义指令，效果相同，只是每次对话都会占用这部分上下文。

## 注意

- Notion 写入通常**不会弹确认**，chatbot 会直接写。skill 规定只有你发出调用时才写；想更稳可以把
  Notion app 的权限设为写入前询问。
- skill 只关联已有资料，不会替聊天里提到的论文新建资料页；想收藏论文请把链接发给 bot。
- 作者主要在 ChatGPT 上使用过；其他端能否自动触发取决于宿主，可能需要在提示里点名 skill。
