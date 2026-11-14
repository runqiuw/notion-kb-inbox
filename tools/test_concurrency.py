"""The event loop must not be blocked by slow ingestion.

Regression for 2026-09-19: an 88s /tasks/process starved an incoming webhook
for 78s because the async handlers did blocking I/O directly on the event loop.
Telegram gave up with "Read timeout expired" and stopped delivering.
"""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from app import config, main

SLOW = 3.0
config.TASKS_SA_EMAIL = ""          # skip OIDC for the test
main._process = lambda uid: time.sleep(SLOW)   # blocking, like the real thing

async def run():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        slow = asyncio.create_task(c.post("/tasks/process", json={"update_id": 1},
                                          timeout=30))
        await asyncio.sleep(0.3)                # let the slow one get going
        t0 = time.monotonic()
        health = await c.get("/_health", timeout=30)
        waited = time.monotonic() - t0
        await slow
        return waited, health

waited, health = asyncio.run(run())
print(f"  slow handler holds the worker for {SLOW}s")
print(f"  concurrent /_health answered in {waited:.2f}s -> {health.json()}")

ok = health.status_code == 200 and waited < SLOW / 2
print("  ok  " if ok else "  FAIL", "event loop stayed responsive during slow work")
print("\n" + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
