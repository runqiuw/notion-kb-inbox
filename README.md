# notion-kb-inbox · Notion 知识库收件箱

把 Telegram 当收集入口，把 Notion 当阅读和思考的界面。
在小红书、知乎、B 站、网页上看到想存的东西，转发给 bot，它会抓正文、读图、用 LLM 写中文概览和要点，
然后在 Notion 里建好一页；你随手发的想法会记成笔记，并能挂到对应的材料上。

> 这是作者个人在用的一套流水线，整理后公开供参考。代码按作者自己的习惯写成，没有为通用场景打磨，
> 可能有 bug。**推荐的用法是交给你的 coding agent，让它读 [DEPLOY.md](DEPLOY.md) 后按你的需求部署和修改。**
>
> **仅供个人收藏使用。** 抓取小红书、知乎、B 站等平台内容时请遵守各平台的用户协议和当地法律，
> 只保存你有权访问的内容，不要用于批量采集、转载或商业用途。

```
Telegram ──webhook──► Cloud Run ──► Firestore（先保存，立即回执）
                          │
                          └─► Cloud Tasks ──► Cloud Run /tasks/process
                                                   │
                                   ┌───────────────┼───────────────┐
                                   ▼               ▼               ▼
                              抓正文 / 读图     GCS 原件归档      Notion 建页
```

三方分工：**Telegram** 负责收集，**Google Cloud**（Cloud Run + Firestore + Cloud Tasks + GCS）负责可靠处理和归档，
**Notion** 负责阅读。闲置时缩到零，GCP 每月花费约 $0.5 以内；LLM 用你自己的 OpenAI 格式端点。

### 两层 Notion，两个入口

```
Telegram ──► bot ──────────────────────────► 🧠 知识库（全部资料 · 全部笔记）
                                                 ▲            │ 关联（只读）
ChatGPT / Claude ── 「知识库：聊天提炼」 写笔记 ──┘            ▼
                └── 「Wiki：查询」 读  ／ 「Wiki：维护」 写 ──► 📖 Wiki（主题条目 · 总索引）
```

- **知识库**：bot 写入的资料和笔记，也是日常阅读界面。部署 bot 只需要这一层。
- **Wiki**（可选）：chatbot 按 skill 把知识库综合成主题条目，回答反复会问的问题；只在 Wiki 一侧关联知识库，不改动原始资料。
- 三个 chatbot skill 都在 Notion 模板里，复制模板后链接自动指向你的副本。见 [wiki-layer.md](docs/wiki-layer.md)、[chat-capture-skill.md](docs/chat-capture-skill.md)。

## 发什么，得到什么

| 你发的 | 结果 |
|---|---|
| 一段文字 | 想法与笔记库里一条随想 |
| 回复某条资料消息说一句话 | 一条笔记，关联那份资料并继承标签，同时显示在资料页「我的想法」下 |
| 链接 | 资料库里一页：中文概览 → 要点 → 折叠原文 → 来源 |
| 带链接的文字 | 只处理链接，**附带文字会被忽略**；想法请单独发一条 |
| 平台分享文案（「复制链接打开小红书…」） | 提取链接，分享话术不当成笔记 |
| 小红书 / 知乎图文帖 | 整体要点 + 逐图总结，OCR 和原文折叠保留，原图归档 |
| 图片、多张图、PDF | 一份资料，识别内容 + 原件归档 |
| arXiv / DOI / 论文项目页 | 论文页：中英对照摘要 |
| B 站 / YouTube 视频 | 按字幕或转写生成章节要点 |
| ChatGPT / Gemini 公开分享链接 | 一条「聊天提炼」：区分你的观点和 AI 的建议，完整对话折叠保留 |
| 已经存过的链接 | `♻️ 已保存过`，不重复建页 |
| 编辑已发出的文字 | 改写对应的笔记 |

进度在同一条消息里就地更新：

```
👀 收到，正在处理…
⏳ 正在抓取 小红书 正文…
⏳ 正在读图并整理…
✅ 已整理 + [打开 Notion]
```

抓不到正文的链接**不会建残缺页面**，而是进暂存区，每天自动重试，也可以手动 `/retry`。

### 支持的来源

| 来源 | 现状 |
|---|---|
| 普通网页 | ✅ trafilatura 抽正文，JS 空壳页用无头浏览器渲染一次 |
| 论文（arXiv / DOI / 项目页 / CVF） | ✅ 走官方 API，只留标题、作者、时间、摘要 |
| 小红书图文 | ✅ 正文 + 多模态逐图总结；视频笔记只存简介 |
| 知乎回答 / 文章 | ✅ 专用解析器，失败用 Jina 免费匿名备用；附评论精华 |
| 图片、PDF（含扫描件） | ✅ |
| B 站 | ⚠️ 只支持有字幕的视频 |
| YouTube | ⚠️ 需要 Gemini 免费 API key |
| ChatGPT / Gemini 分享 | ✅ 纯文字会话 |
| 微信公众号 | ❌ 服务器 IP 被拦，改发截图 |
| X / 微博 / 抖音等 | ⚠️ parsehub 有解析器，未实测 |
| 语音、视频附件、播客 | ❌ 会明确回复「没有存下来」 |

细节见 [docs/sources.md](docs/sources.md)。

### Telegram 命令

- `/staged` — 查看暂存和失败的消息及原因
- `/retry` — 重试暂存链接，重跑整理失败的材料
- `/tags` — 管理标签词表；回复一条已入库的消息发 `/tags` 可改那一页的标签
- `/llm` — 切换 LLM 服务商和模型；`/llm add <名字> <base_url> <模型> <key>` 登记新的（**发完删掉这条消息**）
- `/help` — 用法

## 设计要点

**先保存，再处理。** webhook 只做三件事：验签、把原始消息写进 Firestore、回一句「收到」。
抓取、读图、模型调用都在 Cloud Tasks 拉起的后台请求里。网站打不开、模型挂了、Notion 限流，
都不会让刚发的东西消失。

**Firestore 是处理状态的记录，Notion 只是阅读界面。** 回复关联、URL 去重、消息编辑、失败重试
都依赖 `Telegram 消息 → 记录 → Notion 页面` 这个映射。Notion 限流 3 req/s、无事务、会被手动改，兼不了这个职。

**机器内容、原始材料、你的想法分开。** 重跑整理时保留你写的正文、标签、阅读状态；
「整理完成」和「已读」是两个独立的字段。

**抓取失效是常态。** 各平台的解析方式随时会变，所以失败一律暂存、可重试，而不是丢掉或建半页。

## 部署

需要：一个已绑卡的 GCP 项目、一个 Telegram 账号、一个 Notion 账号、一个**支持视觉**的 OpenAI 格式 LLM 端点。

👉 **[DEPLOY.md](DEPLOY.md)**：完整步骤、需要你本人操作的地方、所有可定制项。

Notion 模板：[一键复制](https://glacier-beard-e5e.notion.site/41a9ba3b59818181a170c5e9c94c6bc8)（或用 `tools/setup_notion.py` 自动建）。
模板包含两层：🧠 知识库（bot 写入的资料与笔记）和 📖 Wiki（可选，chatbot 按 skill 维护的主题条目，见 [wiki-layer.md](docs/wiki-layer.md)）。

分平台说明：[Notion](docs/setup-notion.md) · [Telegram](docs/setup-telegram.md) · [Google Cloud](docs/setup-gcp.md)

本地离线测试（不需要任何密钥）：

```bash
uv venv .venv && . .venv/bin/activate && uv pip install -r requirements.txt
python tools/run_regressions.py
```

## 文档

| 文件 | 内容 |
|---|---|
| [DEPLOY.md](DEPLOY.md) | 部署与定制指南（可直接交给 coding agent） |
| [docs/setup-notion.md](docs/setup-notion.md) · [setup-telegram.md](docs/setup-telegram.md) · [setup-gcp.md](docs/setup-gcp.md) | 三方配置 |
| [docs/notion-schema.md](docs/notion-schema.md) | 数据库字段、建议视图、页面结构、Firestore 集合 |
| [docs/sources.md](docs/sources.md) | 各平台怎么采集、为什么会失败 |
| [docs/tags.md](docs/tags.md) | 标签词表与 `/tags` |
| [docs/video.md](docs/video.md) | B 站字幕与 YouTube 转写 |
| [docs/chat-shares.md](docs/chat-shares.md) | ChatGPT / Gemini 分享会话导入 |
| [docs/chat-capture-skill.md](docs/chat-capture-skill.md) | 在 ChatGPT 等 chatbot 里直接把聊天提炼进知识库（模板自带 Notion skill，或用[插件](chatgpt-plugin/)） |
| [docs/wiki-layer.md](docs/wiki-layer.md) | 知识库之上的 Wiki 层：主题条目、查询与维护 skill（[skill 文本](wiki-skills/)） |
| [docs/operations.md](docs/operations.md) | 排查、轮换密钥、成本、踩过的坑 |
| [docs/backup.md](docs/backup.md) | 备份与隔离恢复（可选） |

## 已知限制

- 单用户设计：一个 Telegram 用户对一套 Notion 库。
- 不处理语音消息、Telegram 内的视频 / 音频附件，没有播客转写。
- Telegram Bot API 下载上限 20 MB。
- 相册旧路径最多处理前 9 张；链接形式的图文帖走分批路径，不受此限。
- 论文的会议名和引用数常为空（Semantic Scholar 匿名限流）。
- 微信公众号在服务器上抓不到。
- Notion 与 Firestore 可能漂移：Firestore 记录丢失而 Notion 页面还在时会重复保存。
- 公开版的代码与作者线上部署一致，但 DEPLOY.md 尚未在全新 GCP 项目里从零完整走过；遇到卡点欢迎提 issue。
- `chatgpt-plugin/` 的 ChatGPT 导入步骤按插件文件格式整理，未在 ChatGPT 中实测；推荐用模板自带的 Notion skill。

## 许可证

[MIT](LICENSE)。`app/assets` 中的平台 Logo 来自 Simple Icons（CC0），商标归各自所有者。
