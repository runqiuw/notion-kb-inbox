"""Import one public share through the bot's normal chat path, without Telegram.

Usage: .venv/bin/python tools/import_chat.py --local-gcloud --url=URL --public-demo
Secrets and current provider settings stay in memory. No database is created.
"""
import os
import argparse
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import backup, chat_notes, config, settings, storage, store, telegram


def configure_note_entry(service, gc):
    """Resolve the production bot name without retaining messaging credentials."""
    env = {e['name']: e for e in service['spec']['template']['spec']['containers'][0]['env']}
    name = env.get('TELEGRAM_BOT_USERNAME', {}).get('value', '')
    if not name:
        item = env.get('TELEGRAM_BOT_TOKEN', {})
        token = item.get('value', '')
        if not token:
            ref = item.get('valueFrom', {}).get('secretKeyRef', {})
            if ref.get('name'):
                token = gc('secrets', 'versions', 'access', ref.get('key', 'latest'),
                           '--secret=' + ref['name']).strip()
        if not token:
            raise RuntimeError('Production bot identity is unavailable; refusing an import without a note entry')
        # getMe is read-only. Keep the token scoped to identity lookup so these
        # administrative tools continue to have no messaging credentials.
        telegram._me_username = None
        with patch.object(config, 'TELEGRAM_BOT_TOKEN', token), patch.object(config, 'TELEGRAM_BOT_USERNAME', ''):
            name = telegram.me_username()
    if not name:
        raise RuntimeError('Production bot username could not be resolved')
    config.TELEGRAM_BOT_USERNAME = name
    telegram._me_username = name


def configure_local(project, *, with_llm=True):
    _, store._db, storage._client = backup.clients(True, project)
    def gc(*parts):
        return subprocess.run(["gcloud", *parts, "--project=" + project], check=True,
                              text=True, capture_output=True).stdout
    service = json.loads(gc("run", "services", "describe", "kb-bot", "--region=us-central1", "--format=json"))
    configure_note_entry(service, gc)
    if not with_llm:
        return
    env = service["spec"]["template"]["spec"]["containers"][0]["env"]
    for e in env:
        if e["name"] in ("LLM_BASE_URL", "LLM_MODEL", "LLM_TIMEOUT") and "value" in e:
            setattr(config, e["name"], float(e["value"]) if e["name"] == "LLM_TIMEOUT" else e["value"])
    if settings.active().get("from_env"):
        config.LLM_API_KEY = gc("secrets", "versions", "access", "latest", "--secret=kb-llm-key").strip()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--public-demo", action="store_true")
    p.add_argument("--local-gcloud", action="store_true")
    p.add_argument("--project", default=os.environ.get("GCP_PROJECT"), required=not os.environ.get("GCP_PROJECT"))
    args = p.parse_args()
    if args.local_gcloud:
        configure_local(args.project)
    msg = {"chat": {"id": 0}, "message_id": "chat-import-" + store.url_hash(args.url)}
    text, url = chat_notes.handle(msg, args.url, args.note, demo=args.public_demo)
    print(json.dumps({"result": text, "notion_url": url}, ensure_ascii=False))
    return 0 if url else 1


if __name__ == "__main__":
    raise SystemExit(main())
