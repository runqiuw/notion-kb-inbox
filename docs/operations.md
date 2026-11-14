# 运维手册

部署之后的日常运维、排查、成本。首次部署见 [DEPLOY.md](../DEPLOY.md)。

下文命令默认服务名 `kb-bot`、区域 `us-central1`，改过的话自行替换。

## 更新部署

只改代码：

```bash
gcloud run deploy kb-bot --source . --region us-central1
```

改了密钥或环境变量：重跑 `./deploy.sh`（幂等）。它会整体替换环境变量，但保留线上已有的
`ZHIHU_DC0`、`S2_API_KEY`、`CROSSREF_MAILTO`、`DYNAMIC_WEB_*`、`USER_TIMEZONE`、`LLM_VLLM_HOSTS`。

部署前跑一遍离线测试：

```bash
python tools/run_regressions.py
```

## 故障排查

### bot 完全没反应

先看 Telegram 有没有在投递：

```bash
TOKEN="$(gcloud secrets versions access latest --secret=kb-telegram-token)"; curl -s "https://api.telegram.org/bot$TOKEN/getWebhookInfo"
```

- `url` 为空 → **webhook 掉了**，多半是刚在 BotFather `/revoke` 过 token（见下文「轮换 bot token」）
- `last_error_message: Read timeout expired` 或 `pending_update_count` 一直涨 → 服务响应太慢

服务侧有没有收到：

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="kb-bot" AND httpRequest.requestUrl!=""' --limit 15 --freshness=30m --format="value(timestamp.date('%H:%M:%S'), httpRequest.requestUrl, httpRequest.status, httpRequest.latency)"
```

日志里 `skipped: not allowed` 说明发消息的人不是 `TELEGRAM_ALLOWED_USER_ID`。

### 某条材料处理失败

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="kb-bot" AND severity>=WARNING' --limit 20 --freshness=1h --format="value(timestamp.date('%H:%M:%S'), textPayload)"
```

在 Telegram 里：

- `/staged`：列出暂存链接、等待 LLM 和处理失败的消息，带原因、尝试次数、时间。每页 4 条，`/staged 2` 翻页。只读。
- `/retry`：先重试暂存链接（成功就正常建页并关联当时的想法），再重跑「需处理」的材料。
  ⚠️ 重跑会刷新标题、材料类型和处理状态。

各来源的常见失败原因见 [sources.md](sources.md)。

### LLM 端点慢或挂了

`/llm test` 看当前服务商的耗时。换一个：

```
/llm add ds https://api.deepseek.com/v1 deepseek-chat sk-你的key
```

**这条消息含明文 key，发完立刻删掉。** `/llm` 弹出列表切换服务商，`/llm model` 拉真实模型列表。

调低单次超时（不会让模型恢复，过短反而增加暂存）：

```bash
gcloud run services update kb-bot --region us-central1 --update-env-vars="LLM_TIMEOUT=30"
```

## 等 LLM 的积压队列

端点不可用时，消息不会被写成残缺页面，而是停在 Firestore `updates` 的 `status = "waiting_llm"`。
Cloud Scheduler `kb-bot-drain` 每 15 分钟调 `/tasks/drain`：先处理等满 3 天的过期项，
再探活端点，活了就重投其余项。

过期的链接进入 `staged`（原因「LLM 过期」），附带想法照常保存，纯文字想法直接建笔记，
图片 / 文件提示你恢复后重发。

```bash
gcloud scheduler jobs run kb-bot-drain --location=us-central1
gcloud run services logs read kb-bot --region=us-central1 --limit=50 | grep drain
```

- `endpoint still down` → 端点问题，先 `/llm test`。
- `could not re-enqueue` → 看任务名的 `-r{n}` 后缀是否撞了 `ALREADY_EXISTS`。

## 每日自动重试

`kb-bot-retry-daily` 每天 03:10（`USER_TIMEZONE`）调 `/tasks/retry`，执行和 `/retry` 相同的流程，
不发 Telegram 消息，结果记在 `auto-retry` 日志。手动和自动重试用 Firestore 租约互斥，单次预算 210 秒。

```bash
gcloud scheduler jobs run kb-bot-retry-daily --location=us-central1
gcloud logging read 'resource.type="cloud_run_revision" AND textPayload:"auto-retry"' --freshness=2d --limit=20
```

## 轮换 bot token

⚠️ **BotFather 的 `/revoke` 会连带清空 webhook。** 轮换后不重新注册，bot 就完全没反应。三步缺一不可：

```bash
printf '%s' '新token' | gcloud secrets versions add kb-telegram-token --data-file=-
```

```bash
gcloud run services update kb-bot --region us-central1 --update-env-vars="TOKEN_ROTATED_AT=$(date -u +%Y%m%dT%H%M%SZ)"
```

```bash
SERVICE_URL="$(gcloud run services describe kb-bot --region us-central1 --format='value(status.url)')" TELEGRAM_BOT_TOKEN="$(gcloud secrets versions access latest --secret=kb-telegram-token)" TELEGRAM_WEBHOOK_SECRET="$(gcloud secrets versions access latest --secret=kb-telegram-secret)" python tools/set_webhook.py
```

第二步的 `TOKEN_ROTATED_AT` 不是装饰：`--set-secrets` 用的 `:latest` **只在创建新 revision 时解析**，
不带配置变更的 update 会被拒绝，服务仍跑着旧 token。其他密钥（Notion、LLM）轮换同理，只需前两步。

## 保活心跳

Cloud Run 在最后一个请求后约 15 分钟回收实例。`kb-bot-keepalive` 每 10 分钟 GET 一次 `/_health`，
避免每天第一条消息等 5–6 秒冷启动。每月约 350 vCPU-秒，在免费额度内。

不想要可以删掉这个 Scheduler 任务。要官方保证常驻，改用 `--min-instances=1`（约 $6–10/月）。

## 成本

按每天存 5 条估：

| 资源 | 月成本 |
|---|---|
| Cloud Run、Firestore、Cloud Tasks、Cloud Scheduler | $0，免费额度内 |
| Cloud Vision（只有 Telegram 单图在用） | $0，每月前 1000 张免费 |
| Artifact Registry | 前 0.5 GB 免费；镜像含 Playwright，可能略超 |
| Secret Manager（5 个密钥） | 约 $0.3 |
| GCS 原件归档 | 约 $0.02/GB |

GCP 合计每月约 $0.5 以内。LLM 调用费用取决于你的服务商，不在这里。

## 延迟

| 环节 | 耗时 |
|---|---|
| 冷启动 | 约 5–6 秒（保活后通常没有） |
| 首条回执「收到」 | 冷启动 + 约 0.2 秒 |
| 抓取正文 | 1–10 秒 |
| **LLM 整理** | **5–60 秒，最大变量** |
| 写入 Notion | 1–3 秒 |

## 踩过的坑

都已经写进 `deploy.sh` 或代码，留在这里方便认出类似症状。

| 症状 | 原因 |
|---|---|
| 健康检查返回 Google 自己的 404 | **`/healthz` 被 Google Front End 吞掉**，到不了容器。改用 `/_health` |
| Cloud Tasks 入队 403 | 服务账号给自己签 OIDC token，需要把 `roles/iam.serviceAccountUser` 绑到它自己身上 |
| 改了密钥服务还在用旧的 | `:latest` 只在新 revision 创建时解析 |
| bot 突然完全没反应 | `/revoke` 连带清空了 webhook |
| webhook 超时 | `async def` 路由里做同步阻塞 I/O 会卡死事件循环，阻塞调用必须走 `run_in_threadpool` |
| 多模态调用报 `Failed to load image` | 小红书 CDN 的 `Content-Type` 谎报格式（实际是 HEIC）。只信 magic bytes，转成 PNG/JPEG 再送模型 |
| 删了 Notion 页面还说「已保存过」 | 去重索引在 Firestore。现在会先确认页面还在 |
| vLLM 网关上 DeepSeek 关不掉思考 | vLLM 用 `chat_template_kwargs.thinking=false`，官方 API 用 `thinking.type=disabled`。把网关主机名填进 `LLM_VLLM_HOSTS` |
