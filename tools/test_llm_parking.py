"""An unreachable LLM parks the message instead of filing a broken page.

Most of this file is the five ways that could go wrong in the plumbing rather
than in the idea: a task name that blocks its own re-dispatch, a parked-URL
index that locks the message out of its own retry, the one command that has to
keep working when the endpoint is down, and the inline path where attempts
never climbs high enough to reach the parking branch.
"""
import os
import sys
import time
import types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx
from app import config, enrich, main, pipeline, store, tasks, telegram
from app import tag_store
# Model outage tests use an isolated formal vocabulary, independent of ADC.
tag_store.load = lambda strict=False: tag_store.seed()
import staging_stub
STAGED = staging_stub.install(store)

config.LLM_USE_SCHEMA = False


# ---------- enrich classifies, and only raises for what can heal ----------
def _endpoint(exc=None, status=200, body='{"title":"t","overview":"o"}'):
    def fake(messages, use_schema, max_tokens=1500):
        if exc:
            raise exc
        if status >= 400:
            raise enrich._Rejected(f"{status} boom", status)
        return body
    return fake

enrich.settings = types.SimpleNamespace(
    active=lambda: {"name": "x", "base_url": "http://x/v1", "model": "m", "api_key": "k"})
enrich.notion = types.SimpleNamespace(known_topics=lambda: ["人工智能"])

raises = [("超时", httpx.ReadTimeout("slow")),
          ("连接失败", httpx.ConnectError("refused"))]
for label, exc in raises:
    enrich._chat = _endpoint(exc=exc)
    try:
        enrich.enrich("t", "正文")
        raise AssertionError(f"{label} 应该抛 EndpointUnavailable")
    except enrich.EndpointUnavailable:
        pass

enrich._chat = _endpoint(status=503)
try:
    enrich.enrich("t", "正文")
    raise AssertionError("5xx 应该抛 EndpointUnavailable")
except enrich.EndpointUnavailable:
    pass

# 4xx and unparseable output will not heal: degrade, do not park.
for status in (401, 422):
    enrich._chat = _endpoint(status=status)
    en, err = enrich.enrich("原标题", "正文")
    assert err and en.title == "原标题", (status, err)
enrich._chat = _endpoint(body="not json at all")
en, err = enrich.enrich("原标题", "正文")
assert "解析失败" in err

# ---------- the liveness probe ----------
enrich._chat = _endpoint(exc=httpx.ConnectError("refused"))
assert enrich.endpoint_alive() is False
enrich._chat = _endpoint(status=401)
assert enrich.endpoint_alive() is True, "4xx means it is up and misconfigured"
enrich._chat = _endpoint()
assert enrich.endpoint_alive() is True


# ---------- ① the task name must change on re-dispatch ----------
names = []
class _FakeClient:
    def queue_path(self, *a): return "q"
    def task_path(self, p, r, q, name): return name
    def create_task(self, parent, task):
        if task["name"] in names:
            raise RuntimeError("ALREADY_EXISTS")
        names.append(task["name"])
config.GCP_PROJECT = config.SERVICE_URL = config.TASKS_SA_EMAIL = "x"
tasks.client = lambda: _FakeClient()
assert tasks.enqueue_process(7) is True
assert tasks.enqueue_process(7) is False, "同名任务应当被拒（这正是问题所在）"
assert tasks.enqueue_process(7, 1) is True, "带重投计数后必须能投出去"
assert tasks.enqueue_process(7, 2) is True
assert names == ["update-7", "update-7-r1", "update-7-r2"]


# ---------- ② the parked-URL index must not lock out its own retry ----------
parked_urls = {}
store.pending_url_owner = lambda url: parked_urls.get(url)
store.park_for_llm = lambda uid, url="": parked_urls.__setitem__(url, uid) if url else None
store.unpark = lambda uid, url="": parked_urls.pop(url, None)

# Past the self-check the handler would go fetch the page for real, so send it
# down the already-saved branch: what is under test is which of the two answers
# comes back, not what happens afterwards.
store.find_by_url = lambda url: {"notion_page_id": "p", "notion_url": "u"}
pipeline.notion = types.SimpleNamespace(page_alive=lambda pid: True)

URL = "https://example.com/a"
parked_urls[URL] = 42
msg = {"chat": {"id": 1}, "message_id": 9, "text": URL}
text, _ = pipeline._handle_url(msg, URL, "", None, update_id=42)
assert "已经在队列里" not in text and "已保存过" in text, \
    f"自己的重试不能被自己写的索引挡住，实际: {text}"
text, _ = pipeline._handle_url(msg, URL, "", None, update_id=99)
assert "已经在队列里" in text, "别人的消息才应该被挡"
parked_urls.clear()


# ---------- ③ /llm test still diagnoses a dead endpoint ----------
enrich._chat = _endpoint(exc=httpx.ConnectError("refused"))
pipeline.settings = types.SimpleNamespace(
    active=lambda: {"name": "x", "base_url": "b", "model": "m", "api_key": "k"})
out, _ = pipeline._handle_llm(["test"], lambda _t: None)
assert "连不上" in out and "失败了" not in out, out

# ...and /retry stops at the first wall instead of walking the whole backlog
tried = []
def _boom(page, say):
    tried.append(page)
    raise enrich.EndpointUnavailable("refused")
pipeline._retry_one = _boom
pipeline.notion = types.SimpleNamespace(
    find_materials=lambda st: [{"id": f"p{i}"} for i in range(30)],
    prop_text=lambda p, n: "t")
store.waiting_for_llm = lambda: []
store.pending_image_jobs = lambda: []
out, _ = pipeline._handle_retry(lambda _t: None)
assert len(tried) == 1, f"应该在第一条就短路，实际撞了 {len(tried)} 条"
assert "LLM 请求未完成" in out and "剩下 30 条" in out


# ---------- ⑤ parking happens on every attempt, not only the last ----------
parked = {}
store.park_for_llm = lambda uid, url="", **kw: parked.__setitem__(uid, parked.get(uid, 0) + 1)
store.get_update = lambda uid: {"payload": {"message": {
    "chat": {"id": 1}, "message_id": 5, "text": "hi"}}, "status": "received"}
store.set_update_status = lambda *a, **k: None
store.bump_attempts = lambda uid: 1          # the inline path never climbs
store.unpark = lambda uid, url="": None
pipeline.dispatch = lambda *a, **k: (_ for _ in ()).throw(
    enrich.EndpointUnavailable("refused"))
telegram.edit = telegram.send = lambda *a, **k: {"message_id": 1}

told = []
telegram.edit = telegram.send = lambda *a, **k: told.append(a[-1] if a else "") or {"message_id": 1}

# Below the threshold: park anyway, then raise so Cloud Tasks retries. The
# parking is the point -- inline processing never climbs past attempts=1, so an
# update parked only at the threshold would never be parked at all.
try:
    main._process(5)
    raise AssertionError("阈值以下应当抛 500 让 Cloud Tasks 继续重试")
except main.HTTPException as e:
    assert e.status_code == 500
assert parked.get(5) == 1, "第一次尝试就该入队，否则 inline 路径永远漏件"
assert not told, "还在重试的阶段不该打扰用户"

# At the threshold: stop, and say so instead of going quiet.
store.bump_attempts = lambda uid: main.LLM_ATTEMPTS
main._process(5)                              # returns 200, no raise
assert parked.get(5) == 2, "达到阈值时也要入队"
assert told and "LLM 请求未完成" in told[-1] and "每 15 分钟" in told[-1], told


# ---------- the drain expires even while the endpoint stays down ----------
store.MAX_PARK_DAYS = 3
fresh = {"update_id": 1, "parked_at": time.time() - 60,
         "payload": {"message": {"chat": {"id": 1}, "message_id": 1}}}
stale = {"update_id": 2, "parked_at": time.time() - 4 * 86400,
         "payload": {"message": {"chat": {"id": 1}, "message_id": 2, "text": URL}}}
store.bump_redispatch = lambda uid: 1
statuses = {}
store.set_update_status = lambda uid, st, err="": statuses.__setitem__(uid, st)
store.waiting_for_llm = lambda: [r for r in [fresh, stale]
                                if statuses.get(r["update_id"]) != "park_expired"]
released = []
store.unpark = lambda uid, url="": released.append((uid, url))

# Endpoint still down: expire the stale link, leave fresh retries alone.
enrich.endpoint_alive = lambda: False
sent = []
tasks.enqueue_process = lambda uid, r=0: sent.append(uid) or True
out = main._drain()
assert out["sent"] == 0 and not sent and out["expired"] == 1, out
assert statuses.get(2) == "park_expired", statuses
assert store.get_staged(URL)["error"] == "LLM 过期"
assert released == [(2, URL)]
assert told and "停止自动整理" in told[-1] and "/retry" in told[-1], told

# Endpoint back: only the fresh one goes out; expiry is not repeated.
enrich.endpoint_alive = lambda: True
told.clear()
out = main._drain()
assert sent == [1], f"只有没过期的该被重投: {sent}"
assert statuses.get(2) == "park_expired", statuses
assert out["expired"] == 0
assert not told and released == [(2, URL)], (told, released)

print("PASS")
