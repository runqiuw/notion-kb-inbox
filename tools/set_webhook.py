"""Point Telegram at the deployed service. Run after deploy.

    SERVICE_URL=https://... TELEGRAM_BOT_TOKEN=... TELEGRAM_WEBHOOK_SECRET=... \
      python tools/set_webhook.py
"""
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, telegram  # noqa: E402

# Shown when the user types "/" in Telegram.
COMMANDS = [
    ("/staged", "查看暂存内容及处理失败原因"),
    ("/tags", "管理标签；回复已入库消息修改标签"),
    ("/llm", "查看或切换 LLM 服务商和模型"),
    ("/retry", "重试暂存区链接，重跑未完成的材料"),
    ("/help", "怎么用"),
]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commands-only", action="store_true", help="更新命令菜单，不重设 webhook")
    args = parser.parse_args()
    if not config.TELEGRAM_BOT_TOKEN or (not args.commands_only and not config.SERVICE_URL):
        sys.exit("need TELEGRAM_BOT_TOKEN and SERVICE_URL")
    if not args.commands_only:
        url = config.SERVICE_URL.rstrip("/") + "/telegram/webhook"
        print(telegram.set_webhook(url, config.TELEGRAM_WEBHOOK_SECRET))
        print("webhook ->", url)
    telegram.set_my_commands(COMMANDS)
    print("commands ->", ", ".join(c for c, _ in COMMANDS))
