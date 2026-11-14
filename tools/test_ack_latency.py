"""The first visible reply must not wait on the Firestore write.

They used to run in sequence; a slow save pushed the "got it" message out by
however long the write took. They now run concurrently, so the reply lands
after max(save, send) rather than save + send.
"""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx
from app import config, main, store, tasks, telegram

SAVE = 1.5   # pretend Firestore is slow (cold client, metadata lookup)
SEND = 0.2

sent_at = []
config.TELEGRAM_WEBHOOK_SECRET = ""
config.TELEGRAM_ALLOWED_USER_ID = ""
store.save_update = lambda uid, p: (time.sleep(SAVE), True)[1]
store.set_update_status = lambda *a, **k: None
store.db = lambda: type("D", (), {"collection": lambda *a: type("C", (), {
    "document": lambda *a: type("Doc", (), {"set": lambda *a, **k: None})()})()})()
tasks.enqueue_process = lambda uid, *a, **k: True

def fake_send(chat_id, text, **kw):
    time.sleep(SEND)
    sent_at.append((time.monotonic(), text))
    return {"message_id": 99}
telegram.send = fake_send

async def run():
    t0 = time.monotonic()
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        await c.post("/telegram/webhook", json={
            "update_id": 77, "message": {"message_id": 1, "chat": {"id": 1},
                                         "from": {"id": 1}, "text": "一条想法"}},
            timeout=30)
    return t0

t0 = asyncio.run(run())
assert sent_at, "no message was sent"
first_reply = sent_at[0][0] - t0

fails = []
def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)

print(f"  save={SAVE}s  send={SEND}s")
print(f"  首条回复耗时 {first_reply:.2f}s   (串行会是 {SAVE+SEND:.2f}s，并行应接近 {SEND:.2f}s)")
check("回复没有等 Firestore", first_reply < SAVE, f"{first_reply:.2f}s")
check("文案是「收到」而非承诺已保存", "收到" in sent_at[0][1], sent_at[0][1])

print("\n" + ("PASS" if not fails else f"FAIL: {fails}"))
sys.exit(1 if fails else 0)
