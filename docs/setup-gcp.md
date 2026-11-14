# Google Cloud 部署

服务跑在 Cloud Run 上，闲置时缩到零。按每天存几条估算，GCP 每月不到 $0.5（见 [运维手册 · 成本](operations.md#成本)）。

## 0. 前提

- 一个已关联结算账号的 GCP 项目（免费额度内也需要绑卡）。
- 本机装好 [gcloud CLI](https://cloud.google.com/sdk/docs/install)，并执行过
  `gcloud auth login`。
- Telegram 与 Notion 两步已完成：[setup-telegram.md](setup-telegram.md)、[setup-notion.md](setup-notion.md)。
- 一个 OpenAI 格式的 LLM 端点（只用 `/v1/chat/completions`），**模型必须支持视觉**，
  图文帖和图片都靠它读图。

## 1. 填环境变量

```bash
cp .env.example .env
```

必填：

| 变量 | 来源 |
|---|---|
| `GCP_PROJECT` | GCP 项目 ID |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_WEBHOOK_SECRET` / `TELEGRAM_ALLOWED_USER_ID` | [Telegram 配置](setup-telegram.md) |
| `NOTION_TOKEN` / `NOTION_MATERIAL_DB` / `NOTION_THOUGHT_DB` | [Notion 配置](setup-notion.md) |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | 你的模型服务 |

可选：`GEMINI_API_KEY`（视频转写）、`S2_API_KEY`（论文引用数）、`ZHIHU_DC0`（知乎访客标识）、
`USER_TIMEZONE`（默认 `Asia/Shanghai`）等，说明见 `.env.example`。

## 2. 一键部署

```bash
set -a && . ./.env && set +a && ./deploy.sh
```

脚本幂等，可以反复跑。它依次做这些事：

| 步骤 | 资源 | 作用 |
|---|---|---|
| 启用 API | Run、Build、Artifact Registry、Firestore、Tasks、Storage、Vision、Secret Manager、Scheduler | |
| 服务账号 | `kb-bot-sa` | 运行身份；同时作为 Cloud Tasks / Scheduler 的 OIDC 身份 |
| Firestore | `(default)`，Native 模式 | 处理状态的 system of record：消息→页面映射、去重、暂存区、重试 |
| GCS 桶 | `<项目>-kb-originals` | 原图、PDF、抓取正文的原件归档 |
| Cloud Tasks | `kb-bot-queue`、`kb-bot-video-queue` | 异步处理；webhook 只落库就返回 |
| Secret Manager | `kb-telegram-token`、`kb-telegram-secret`、`kb-notion-token`、`kb-llm-key`、`kb-gemini-key` | 密钥只从你的 shell 读入，不落盘、不进镜像 |
| Cloud Run | `kb-bot` | `--source .` 用 Dockerfile 构建；1 vCPU / 1 GiB / 最多 3 实例 |
| Cloud Scheduler | `kb-bot-keepalive`（10 分钟） | 保温，避免冷启动 |
| | `kb-bot-drain`（15 分钟） | 唤醒因 LLM 不可用而排队的消息 |
| | `kb-bot-retry-daily`（每天 03:10） | 自动重试暂存区 |
| Telegram | webhook + 命令菜单 | 调用 `tools/set_webhook.py` |

> 第一次部署时 Firestore 建库要几分钟；如果某一步报 `not ready`，等一会儿重跑即可。

## 3. 验证

```bash
URL=$(gcloud run services describe kb-bot --region us-central1 --format='value(status.url)')
curl -s "$URL/_health"
```

然后在 Telegram 给 bot 发一个普通网页链接，几十秒内应在 Notion 资料库看到新页面。

看日志：

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="kb-bot" AND severity>=WARNING' --freshness=1h --limit=30
```

## 4. 之后改代码

```bash
gcloud run deploy kb-bot --source . --region us-central1
```

只改代码时用这条就够；改了密钥或环境变量再跑 `./deploy.sh`。
注意 `deploy.sh` 会整体替换环境变量，但会保留线上已有的 `ZHIHU_DC0`、`S2_API_KEY` 等可选值。

## 5. 可选：每日备份

备份是独立的 Cloud Run Job，要求先跑通一次隔离恢复演练，见 [backup.md](backup.md)。

## 清理

不想用了：删除 Cloud Run 服务、Scheduler 任务、Tasks 队列、GCS 桶和 Secret，或直接删除整个项目。
