"""The per-platform fallback parser, and the rule that its failure is routine.

trafilatura handles ordinary pages. Sites that 403 a plain GET (知乎, 小红书)
skip it and go to parsehub, which reads a page's embedded state or replays the
site's request signature -- both break when the site changes, so a failure must
degrade to the staging area rather than lose the link.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, enrich, extract, notion, pipeline, storage, store
pipeline.jina_reader.fetch = lambda url: {'ok': False, 'error': '备用阅读接口也失败'}
import staging_stub
STAGED = staging_stub.install(store)

mats, calls = [], []
notion.create_material = lambda **kw: mats.append(kw) or {"id": "m", "url": "https://n/m"}
notion.create_thought = lambda **kw: {"id": "t", "url": "https://n/t"}
notion.body_location = lambda p: (p["id"], ["b"])
storage.archive_text = storage.archive = lambda *a, **k: "gs://b/x"
# Keep the real in-memory record checkpoint installed by staging_stub.
store.find_by_url = lambda u: None
store.index_url = lambda *a: None
store.pending_url_owner = lambda url: None   # 没有链接在等 LLM
store.waiting_for_llm = lambda: []
enrich.enrich = lambda t, x, h="", images=None, note="": (enrich.Enrichment(title=t or "标题", overview="概览"), "")

def msg(mid, text):
    return {"message_id": mid, "chat": {"id": 1}, "from": {"id": 1}, "text": text}

fails = []
def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)

print("1. 普通网页：走 trafilatura，不碰 parsehub")
extract.fetch_article = lambda u: {"ok": True, "error": "", "title": "T", "text": "正文" * 40}
extract.parsehub_platform = lambda u: calls.append(("platform", u)) or "x"
extract.parse_via_parsehub = lambda u, c=None: calls.append(("parse", u)) or {"ok": False}
pipeline.dispatch(msg(1, "https://example.com/a"), False)
check("没有调用 parsehub", not any(c[0] == "parse" for c in calls), str(calls))
check("处理状态=完成", mats[-1]["processing"] == "完成")

print("2. 知乎：跳过 trafilatura，直接用专用解析器")
calls.clear()
touched = []
extract.fetch_article = lambda u: touched.append(u) or {"ok": False, "error": "x", "title": "", "text": ""}
extract.parse_via_parsehub = lambda u, c=None: {"ok": True, "title": "知乎文章标题",
                                                "text": "知乎正文" * 30, "platform": "zhihu",
                                                "images": [], "error": ""}
pipeline.dispatch(msg(2, "https://zhuanlan.zhihu.com/p/123"), False)
check("没浪费一次必然 403 的请求", touched == [], str(touched))
check("拿到了正文", mats[-1]["title"] == "知乎文章标题", mats[-1]["title"])
check("处理状态=完成", mats[-1]["processing"] == "完成")

print("3. 两条路都失败：进暂存区，不建空页，链接不丢")
extract.parse_via_parsehub = lambda u, c=None: {"ok": False, "title": "", "text": "",
                                                "platform": "", "error": "签名失效"}
n = len(mats)
t, u = pipeline.dispatch(msg(3, "https://zhuanlan.zhihu.com/p/456"), False)
check("没有建只有链接的页", len(mats) == n)
row = store.get_staged("https://zhuanlan.zhihu.com/p/456")
check("原始链接进了暂存区", bool(row), str(STAGED))
check("失败原因留在暂存区并告诉用户", row and "签名失效" in row["error"] and "签名失效" in t, t)

print("3b. /retry 成功后正常建页并移出暂存区")
extract.parse_via_parsehub = lambda u, c=None: {"ok": True, "title": "补上的知乎", "text": "知乎正文" * 30,
                                                "platform": "zhihu", "images": [], "error": ""}
notion.find_materials = lambda st, limit=50: []
store.waiting_for_llm = lambda: []
t, u = pipeline.dispatch(msg(33, "/retry"), False)
check("建了页", len(mats) == n + 1 and mats[-1]["url"] == "https://zhuanlan.zhihu.com/p/456")
check("暂存区清空", not STAGED, str(STAGED))
check("报告成功", "1 条成功" in t, t)

print("3c. 视频没有转写：再长的简介也不算采集完成，视频走视频任务")
extract.fetch_article = lambda u: {"ok": True, "error": "", "title": "某视频 - YouTube", "text": "很长的简介" * 100}
VIDEO_JOBS = {}
store.claim_video_job = lambda key: "owner"
store.release_video_job = lambda key, owner: None
store.get_video_job = lambda key: VIDEO_JOBS.get(key)
store.update_video_job = lambda job_key, **f: VIDEO_JOBS.setdefault(job_key, {}).update(f)
config.GEMINI_API_KEY = ""   # no transcriber configured: must stage, not scrape
n = len(mats)
t, u = pipeline.dispatch(msg(34, "https://www.youtube.com/watch?v=abcdefghijk"), False)
check("没有建书签页", len(mats) == n)
check("进暂存区并说明原因", "转录" in t and store.get_staged("https://www.youtube.com/watch?v=abcdefghijk"), t)
check("没有用网页抓取代替视频文本", pipeline.fetch_for("https://youtu.be/abcdefghijk")["ok"] is False)

print("3d. 暂存想法关联失败：保留暂存记录，/retry 补上")
STAGED.clear()
extract.parsehub_platform = lambda u: None
url = "https://example.com/flaky"
extract.fetch_article = lambda u: {"ok": False, "error": "timeout", "title": "", "text": ""}
pipeline.dispatch(msg(35, url + " 这个结论和上周读的那篇正好相反"), False)
row = store.get_staged(url)
check("附带文字被忽略", row and not row.get('thought_ids'), str(row))
legacy = notion.create_thought(title='旧笔记', note_type='随想', topics=[], related=[], blocks=notion.paragraphs('已有明确引用的旧笔记'))
store.stage_url(url, 1, 35, 'timeout', [legacy['id']])
row = store.get_staged(url)
store.update_chat_stage(url, explicit_thought_ids=row['thought_ids'])  # Explicit reply authorization fixture.
real_get_page = notion.get_page
notion.get_page = lambda pid: {'id': pid, 'properties': {}}
extract.fetch_article = lambda u: {"ok": True, "error": "", "title": "T", "text": "正文" * 40}
real_link = notion.link_thought_to_material
notion.link_thought_to_material = lambda tid, ids: (_ for _ in ()).throw(RuntimeError("502"))
pipeline._handle_retry(lambda t: None)
row = store.get_staged(url)
check("资料建页了，但暂存记录还在", row and row.get("page_id") and row["thought_ids"], str(row))
linked = []
notion.link_thought_to_material = lambda tid, ids: linked.append((tid, ids))
n = len(mats)
pipeline._handle_retry(lambda t: None)
check("再次 /retry 只补关联，不重复建页", len(mats) == n and len(linked) == 1, f"{linked} {len(mats)-n}")
check("补完后移出暂存区", not store.get_staged(url), str(STAGED))
notion.link_thought_to_material = real_link
notion.get_page = real_get_page

print("4. 知乎 cookie 只取 d_c0，绝不带 z_c0")
config.ZHIHU_DC0 = "DEVICE123"
ck = extract.parse_cookie_for("https://zhuanlan.zhihu.com/p/1")
check("只有 d_c0 一个字段", ck == {"d_c0": "DEVICE123"}, str(ck))
check("非知乎站点不带 cookie", extract.parse_cookie_for("https://example.com") is None)
config.ZHIHU_DC0 = ""
check("没配 d_c0 就不传", extract.parse_cookie_for("https://zhuanlan.zhihu.com/p/1") is None)

print("\n" + ("PASS" if not fails else f"FAIL: {fails}"))
sys.exit(1 if fails else 0)
