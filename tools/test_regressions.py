"""Regressions for the three bugs found on the first real run (2026-09-19)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import enrich, pipeline, telegram

fails = []
def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)

print("BUG 1: bot token must never reach a log line")
leak = 'HTTP Request: POST https://api.telegram.org/bot1234567890:AAFAKEfakeFAKEfakeFAKEfakeFAKEfake0/sendMessage'
red = telegram.redact(leak)
check("token removed", "AAFAKEfakeFAKEfakeFAKEfakeFAKEfake0" not in red, red)
check("digits removed too", "1234567890" not in red, red)
check("rest of message intact", "sendMessage" in red, red)
check("file-download urls too",
      "SECRET" not in telegram.redact("https://api.telegram.org/file/bot123:SECRET/photo.jpg"))
import logging
check("httpx logger silenced at INFO",
      logging.getLogger("httpx").level >= logging.WARNING)

print("BUG 2: /start must not become a thought")
for c in ["/start", "/help", "/start@my_bot", "  /foo bar"]:
    check(f"{c!r} is a command", pipeline.is_command(c))
for t in ["world model 更适合承担高层 prediction", "https://arxiv.org/abs/1803.10122",
          "3/4 的材料还没读"]:
    check(f"{t[:20]!r} is content", not pipeline.is_command(t))

# the webhook must answer commands without saving or enqueuing anything
from fastapi.testclient import TestClient
from app import config, main, store, tasks
config.TELEGRAM_WEBHOOK_SECRET = ""
config.TELEGRAM_ALLOWED_USER_ID = ""
saved, sent, queued = [], [], []
store.save_update = lambda uid, p: saved.append(uid) or True
tasks.enqueue_process = lambda uid, *a, **k: queued.append(uid) or True
telegram.send = lambda cid, text, **kw: sent.append(text) or {"message_id": 1}

r = TestClient(main.app).post("/telegram/webhook", json={
    "update_id": 1, "message": {"message_id": 1, "chat": {"id": 1},
                                "from": {"id": 1}, "text": "/start"}})
check("webhook reports skipped", r.json().get("skipped") == "command", str(r.json()))
check("nothing written to firestore", saved == [], str(saved))
check("nothing enqueued", queued == [], str(queued))
check("help text sent back", len(sent) == 1 and "链接" in sent[0])

# a photo caption starting with "/" is still content, not a command
r = TestClient(main.app).post("/telegram/webhook", json={
    "update_id": 2, "message": {"message_id": 2, "chat": {"id": 1}, "from": {"id": 1},
                                "caption": "/tmp 下的截图", "photo": [{"file_id": "f"}]}})
check("photo caption is not a command", r.json().get("skipped") != "command", str(r.json()))

print("BUG 3: topic vocabulary must not drift")
check("normalizes specific tags and drops retired generic tags",
      enrich._cap_topics(["人工智能", "强化学习", "神经网络"]) == ["RL"],
      str(enrich._cap_topics(["人工智能", "强化学习", "神经网络"])))
check("all-known passes through",
      enrich._cap_topics(["Agent", "Workflow"]) == ["Agent", "Workflow"])
check("unknown tags do not grow vocabulary", enrich._cap_topics(["甲", "乙", "丙"]) == [])
check("blanks dropped", enrich._cap_topics(["", "Agent"]) == ["Agent"])
check("hard cap of 3",
      len(enrich._cap_topics(["Agent", "RL", "Multimodal", "Tool"])) == 3)

print("\n" + ("PASS" if not fails else f"FAIL: {fails}"))
sys.exit(1 if fails else 0)
