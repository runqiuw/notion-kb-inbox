# Telegram 配置

Bot 只是收集入口：你转发什么，它就存什么。只服务你一个人，其他人发的消息会被忽略。

## 1. 建 bot

1. 在 Telegram 里找 **@BotFather**，发 `/newbot`，按提示起名字和用户名（必须以 `bot` 结尾）。
2. 记下它给的 token，形如 `1234567890:AA...`，填到 `TELEGRAM_BOT_TOKEN`。
   **token 等于 bot 的全部权限，不要提交进 Git，也不要发到群里。**
3. 可选：`/setuserpic`、`/setdescription` 设置头像和简介。

## 2. 拿你自己的 user id

找 **@userinfobot**，随便发一句话，它会回你的数字 id，填到 `TELEGRAM_ALLOWED_USER_ID`。

留空等于允许所有人使用，只适合本地调试。部署后任何人都能搜到你的 bot，务必填上。

## 3. webhook 密钥

```bash
openssl rand -hex 24
```

结果填到 `TELEGRAM_WEBHOOK_SECRET`。Telegram 每次推送都会带上这个值，服务端用它验签，
防止别人伪造请求打到你的 Cloud Run 地址。

## 4. 注册 webhook 与命令菜单

`deploy.sh` 跑完最后一步会自动执行 `tools/set_webhook.py`，把 webhook 指向
`<服务地址>/telegram/webhook`，并注册 `/staged`、`/tags`、`/llm`、`/retry`、`/help` 菜单。

只改菜单文案时：

```bash
TELEGRAM_BOT_TOKEN=... python tools/set_webhook.py --commands-only
```

**不要随手重跑整套 webhook 设置。** 密钥填错不会报错，bot 只会悄悄收不到消息。

## 5. 验证

```bash
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"
```

看 `url` 是否是你的服务地址、`last_error_message` 是否为空。然后给 bot 发一个链接，
应该先看到「👀 收到，正在处理…」，最后变成「✅ 已整理 + 打开 Notion」。

没反应时按 [运维手册](operations.md#bot-完全没反应) 排查。

## 6. 换 token

token 泄露后：@BotFather → `/revoke` 拿新 token → 更新 Secret Manager 里的
`kb-telegram-token` → 重新部署。步骤见 [运维手册](operations.md#轮换-bot-token)。
