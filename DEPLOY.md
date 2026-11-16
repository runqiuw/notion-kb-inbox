# 部署与定制指南（写给你和你的 coding agent）

这份文档的用法：**把仓库交给你的 coding agent（Claude Code、Codex、Cursor……），让它先读这一份，
再告诉它你的需求**，例如：

> 读一下 DEPLOY.md，帮我部署这个 bot。我主要看小红书和 B 站，标签换成设计相关的，时区用纽约，
> 不需要视频功能，LLM 用 DeepSeek 官方 API。

agent 按下面的步骤推进，遇到需要你本人操作的地方（标 🙋）会停下来让你做。

---

## 0. 先确认这几件事

| 问题 | 影响 |
|---|---|
| 有没有已绑卡的 GCP 项目？ | 必须有。免费额度内基本不花钱，但 Cloud Run 要求绑定结算账号 |
| LLM 用哪家？模型支持视觉吗？ | 必须是 OpenAI 格式的 `/v1/chat/completions`，**必须支持图片输入**（图文帖、图片都靠它） |
| 需要 YouTube 视频吗？ | 需要的话准备一个 Gemini API key（建议建在**不绑卡**的项目里） |
| 你主要收藏哪些平台？ | 决定要不要配 `ZHIHU_DC0`、动态网页渲染、视频等 |
| 你关心哪些领域？ | 决定标签词表（默认是一份通用科技阅读示例） |
| 摘要用什么语言？ | 默认中文。改语言要改提示词，见第 4 节 |

## 1. 本地准备

```bash
git clone https://github.com/runqiuw/notion-kb-inbox.git && cd notion-kb-inbox
uv venv .venv && . .venv/bin/activate && uv pip install -r requirements.txt
python tools/run_regressions.py      # 全部离线，不需要任何密钥
cp .env.example .env
```

`test_cover_art.py` 需要系统有 cairo（macOS：`brew install cairo`），没有可以忽略。

## 2. 三方配置（按顺序）

### 2.1 Notion → [docs/setup-notion.md](docs/setup-notion.md)

1. 🙋 在 notion.so/profile/integrations 建 Internal integration，复制 token 到 `.env` 的 `NOTION_TOKEN`。
2. 🙋 新建一个空页面，`···` → Connections 加入这个 integration，把页面链接给 agent。
3. 二选一：
   - 🙋 复制 [Notion 模板](https://glacier-beard-e5e.notion.site/41a9ba3b59818181a170c5e9c94c6bc8)
     到该页面下，把两个数据库链接给 agent，agent 从链接里取 32 位 ID（见 setup-notion.md 3A）。
     模板里的 📖 Wiki 层是可选的，bot 不依赖它，需要的话部署后照 [wiki-layer.md](docs/wiki-layer.md) 接入 chatbot。
   - agent 运行脚本（没有视图和按钮，之后可照模板手工补）：

     ```bash
     set -a && . ./.env && set +a
     python tools/setup_notion.py --parent <页面链接>
     ```

   把 `NOTION_MATERIAL_DB`、`NOTION_THOUGHT_DB` 写进 `.env`。

### 2.2 Telegram → [docs/setup-telegram.md](docs/setup-telegram.md)

1. 🙋 找 @BotFather `/newbot`，token 写进 `TELEGRAM_BOT_TOKEN`。
2. 🙋 找 @userinfobot 拿自己的数字 id，写进 `TELEGRAM_ALLOWED_USER_ID`。**不填等于任何人都能用你的 bot。**
3. agent 生成 webhook 密钥：`openssl rand -hex 24` → `TELEGRAM_WEBHOOK_SECRET`。

### 2.3 LLM

写进 `LLM_BASE_URL`（到 `/v1` 为止）、`LLM_MODEL`、`LLM_API_KEY`。
网关不支持 `response_format=json_schema` 时设 `LLM_USE_SCHEMA=0`（代码会自动降级，但设了更快）。
部署后可以在 Telegram 里用 `/llm add`、`/llm` 增加和切换服务商，不用重新部署。

### 2.4 Google Cloud → [docs/setup-gcp.md](docs/setup-gcp.md)

1. 🙋 `gcloud auth login`（需要浏览器登录，agent 做不了）。
2. agent 运行：

   ```bash
   set -a && . ./.env && set +a && ./deploy.sh
   ```

   脚本幂等。它会启用 API、建服务账号、Firestore、GCS 桶、两个 Cloud Tasks 队列、Secret、
   Cloud Run 服务、三个 Scheduler 任务，最后注册 Telegram webhook。
   第一次建 Firestore 可能要等几分钟，报 not ready 就重跑。

## 3. 验收

agent 能自己检查的：

```bash
URL=$(gcloud run services describe kb-bot --region us-central1 --format='value(status.url)')
curl -s "$URL/_health"
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"   # url 正确、无 last_error_message
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="kb-bot" AND severity>=WARNING' --freshness=30m --limit=20
```

🙋 需要你在 Telegram 里发的（按需挑）：

| 发什么 | 期望 |
|---|---|
| 一句话 | 想法与笔记库出现一条随想 |
| 一个普通网页链接 | 资料库出现新页面：中文概览、要点、折叠原文 |
| 回复上一条链接消息说一句话 | 笔记关联到那条资料，并镜像显示在资料页「我的想法」下 |
| 一个 arXiv 链接 | 论文页：中英对照摘要 |
| 一个小红书图文链接 | 逐图总结 |
| 同一个链接再发一次 | 回复「已保存过」 |
| `/staged`、`/tags`、`/llm` | 弹出对应面板 |

## 4. 可以定制的地方

| 想改什么 | 改哪里 | 注意 |
|---|---|---|
| 服务名 / 区域 / 队列名 / 桶名 | 环境变量 `SERVICE`、`GCP_REGION`、`TASKS_QUEUE`、`VIDEO_TASKS_QUEUE`、`GCS_BUCKET` | `docs/` 里的命令默认 `kb-bot` / `us-central1`；Scheduler 任务名写死为 `kb-bot-*` |
| 实例规格、并发、超时 | `deploy.sh` 的 `gcloud run deploy` 参数 | 动态网页渲染需要 1 GiB；视频单次请求可能几分钟，超时别低于 900 秒 |
| 时区 | `USER_TIMEZONE`（默认 `Asia/Shanghai`） | 影响回复里的时间和每日重试时间 |
| 定时任务 | `deploy.sh` 末尾：`kb-bot-keepalive`（10 分钟）、`kb-bot-drain`（15 分钟）、`kb-bot-retry-daily`（03:10） | 不想保温可删 keepalive，代价是冷启动 5–6 秒 |
| 标签词表 | `app/topics.py` 的 `GROUPS`、`_ALIASES` | **只在首次运行时写入 Firestore**；之后用 Telegram `/tags` 改。删 `ML`/`LLM`/`MLSys`/`Systems` 时要改相关规则，见 [tags.md](docs/tags.md) |
| 摘要语言与风格 | `app/enrich.py` 的 `SYSTEM` 等提示词；聊天提炼在 `app/chat_notes.py`；视频章节在 `app/video.py` | 页面小节标题（「中文概览」等）写在 `app/notion.py`、`app/pipeline.py` |
| Bot 回复文案、命令菜单 | `app/pipeline.py`、`app/main.py`；菜单在 `tools/set_webhook.py` 的 `COMMANDS` | 改菜单后 `python tools/set_webhook.py --commands-only` |
| Notion 属性名 / 选项名 | `app/notion.py`、`app/pipeline.py` 及 `tools/setup_notion.py` | **强耦合**：三处必须一致，否则写入 400。不建议改，根页面和数据库标题可以随便改 |
| 卡片占位图、平台图标 | `app/assets/`、`app/thumbnails.py`、`app/art_categories.py` | |
| 动态网页渲染 | `DYNAMIC_WEB_ENABLED=0` 关闭 | 关掉后镜像里的 Playwright 可以从 `Dockerfile`、`requirements.txt` 删掉，镜像会小很多 |
| 知乎 | `ZHIHU_DC0`（可选访客标识）、`JINA_READER_ENABLED=0` 关闭 Jina 备用 | Jina 是第三方，内容会经过它 |
| 视频 | 不配 `GEMINI_API_KEY` 则 YouTube 链接进暂存区；B 站字幕不需要 key | 见 [video.md](docs/video.md) |
| 论文元数据 | `S2_API_KEY`、`CROSSREF_MAILTO` | 不填也能用，只是会议名和引用数常为空 |
| vLLM 网关上的 DeepSeek | `LLM_VLLM_HOSTS=<主机名>` | 让「关闭思考」用对参数 |
| 备份 | 可选，见 [backup.md](docs/backup.md) | 需要先手工建桶和服务账号 |
| 多用户 | 不支持 | 设计上是单人 bot：一个 `TELEGRAM_ALLOWED_USER_ID` 对一套 Notion 库 |

## 5. 给 agent 的注意事项

- **不要把 `.env` 提交进 Git。** 密钥只通过 `deploy.sh` 从 shell 读入 Secret Manager。
- **`deploy.sh` 用 `--set-env-vars` 整体替换环境变量。** 新增可选变量时，要么加进 `deploy.sh`
  的 `keep_env` 和 `--set-env-vars`，要么用 `gcloud run services update --update-env-vars` 单独加。
- **不要随手重跑 `tools/set_webhook.py`。** 密钥填错不会报错，bot 只会悄悄收不到消息。
- 改代码前先跑 `python tools/run_regressions.py`，改完再跑一遍；只改代码时用
  `gcloud run deploy kb-bot --source . --region us-central1` 部署就够了。
- 外部平台的抓取方式（小红书、知乎、B 站、微信）随时会失效，**这是常态**。失败的材料会进暂存区而不是丢失，
  修好后 `/retry` 即可。各来源的机制见 [sources.md](docs/sources.md)。
- 排查从 [operations.md](docs/operations.md) 开始。

## 6. 代码地图

| 文件 | 作用 |
|---|---|
| `app/main.py` | FastAPI 入口：webhook 验签、落库、回执；`/tasks/*` 后台任务入口 |
| `app/pipeline.py` | 消息分发：文字 / 链接 / 图片 / 文件 / 回复 / 编辑，`/retry`、`/staged` |
| `app/extract.py` | 抓取：网页、论文 API、小红书、知乎、微信 |
| `app/render_web.py` | Playwright 动态网页兜底 |
| `app/jina_reader.py` | 知乎的 Jina 备用 |
| `app/enrich.py` | 唯一的 LLM 整理步骤：标题、概览、要点、标签 |
| `app/image_batches.py` | 多图帖子分批读图与续跑 |
| `app/video.py` | B 站字幕、YouTube Gemini 转写 |
| `app/chat_share.py`、`app/chat_notes.py` | ChatGPT / Gemini 分享会话 |
| `app/paper_project.py` | 论文项目页识别 |
| `app/comments.py` | 知乎评论精华 |
| `app/notion.py` | Notion 读写、页面拼块 |
| `app/store.py` | Firestore |
| `app/storage.py` | GCS 原件归档 |
| `app/tasks.py` | Cloud Tasks 入队 |
| `app/settings.py` | `/llm` 服务商配置 |
| `app/topics.py`、`app/tag_*.py` | 标签词表与 `/tags` |
| `app/thumbnails.py`、`app/pdf_art.py` | 卡片缩略图与页面图标 |
| `app/backup.py`、`app/restore.py` | 备份与隔离恢复 |
| `tools/test_*.py` | 离线测试，`tools/run_regressions.py` 一次全跑 |
| `tools/setup_notion.py` | 建 Notion 数据库 |
| `tools/set_webhook.py` | 注册 webhook 与命令菜单 |
