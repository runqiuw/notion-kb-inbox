"""Dedupe must not outlive the page it points at.

The index lives in Firestore; Notion has no idea about it and vice versa. When
the user deleted a material in Notion, the bot kept answering 已保存过 about a
page that no longer existed, and the link could never be saved again.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import enrich, extract, notion, pipeline, storage, store
import staging_stub
STAGED = staging_stub.install(store)

fails = []
def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)

URL = "https://xhslink.cn/o/abc"
mats, thoughts, forgotten = [], [], []
notion.create_material = lambda **kw: mats.append(kw) or {"id": "m-new", "url": "https://n/new"}
notion.create_thought = lambda **kw: thoughts.append(kw) or {"id": "t", "url": "https://n/t"}
notion.body_location = lambda p: (p["id"], ["b"])
storage.archive = storage.archive_text = lambda *a, **k: ""
store.put_record = lambda c, m, **f: f"{c}:{m}"
store.index_url = lambda *a: None
store.forget_url = lambda u: forgotten.append(u)
extract.plain_http_blocked = lambda u: False
extract.parsehub_platform = lambda u: None
extract.fetch_article = lambda u: {"ok": True, "error": "", "title": "T", "text": "正文" * 40}
enrich.enrich = lambda t, x, h="", images=None, note="": (enrich.Enrichment(title="标题", overview="概览"), "")

def msg(mid, text=URL):
    return {"message_id": mid, "chat": {"id": 1}, "from": {"id": 1}, "text": text}

REAL_PAGE_ALIVE = notion.page_alive   # steps 3-4 test the real thing

print("1. 页面还在 → 照常去重")
store.find_by_url = lambda u: {"notion_page_id": "m-old", "notion_url": "https://n/old"}
store.pending_url_owner = lambda url: None   # 没有链接在等 LLM
store.waiting_for_llm = lambda: []
notion.page_alive = lambda pid: True
t, _ = pipeline.dispatch(msg(1), False)
check("回复已保存过", "♻️" in t, t)
check("没有重复建页", mats == [], str(mats))
check("没有误删索引", forgotten == [], str(forgotten))

print("2. 页面被删了 → 丢掉索引，当新材料处理")
notion.page_alive = lambda pid: False
t, _ = pipeline.dispatch(msg(2), False)
check("索引被清掉", forgotten == [URL], str(forgotten))
check("重新建了材料", len(mats) == 1, str(len(mats)))
check("回复不再是已保存过", "♻️" not in t, t)

print("3. 页面在回收站里 → 同样视为已删")
notion.page_alive = REAL_PAGE_ALIVE
notion._call = lambda m, p, b=None: {"archived": True}
check("in trash 判定为不存在", notion.page_alive("x") is False)
notion._call = lambda m, p, b=None: {"archived": False, "in_trash": False}
check("正常页面判定为存在", notion.page_alive("x") is True)

print("4. 网络抖动不能被当成删除")
def boom(m, p, b=None): raise RuntimeError("notion GET /pages/x -> 503 upstream")
notion._call = boom
try:
    notion.page_alive("x"); ok = False
except RuntimeError:
    ok = True
check("5xx 会抛出而不是谎报已删", ok)
def gone(m, p, b=None): raise RuntimeError("notion GET /pages/x -> 404 object_not_found")
notion._call = gone
check("404 才算已删", notion.page_alive("x") is False)

print("\n" + ("PASS" if not fails else f"FAIL: {fails}"))
sys.exit(1 if fails else 0)
