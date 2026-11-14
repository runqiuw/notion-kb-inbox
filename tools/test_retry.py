"""/retry must rescue 需处理 materials without touching anything the user wrote.

Before this existed, a material whose tidying failed (an LLM timeout, say) sat
in 需处理 forever -- the status was a graveyard, not a queue.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import enrich, extract, notion, pipeline, store
import staging_stub
STAGED = staging_stub.install(store)
store.pending_url_owner = lambda url: None   # 没有链接在等 LLM
store.waiting_for_llm = lambda: []

fails = []
def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)

def page(pid, title, url, status="需处理"):
    return {"id": pid, "properties": {
        "标题": {"type": "title", "title": [{"plain_text": title}]},
        "原始链接": {"type": "url", "url": url},
        "处理状态": {"type": "select", "select": {"name": status}}}}

refreshed, appended, other_updates = [], [], []
notion.refresh_machine_fields = lambda pid, **kw: refreshed.append((pid, kw))
notion.append_blocks = lambda pid, blocks: appended.append((pid, blocks))
notion.update_properties = lambda pid, props: other_updates.append((pid, props))
extract.plain_http_blocked = lambda u: False
extract.parsehub_platform = lambda u: None
extract.fetch_article = lambda u: {"ok": True, "error": "", "title": "抓到的", "text": "正文" * 50}
enrich.enrich = lambda t, x, h="": (
    enrich.Enrichment(title="整理后的中文标题", overview="这是补上的概览。",
                      material_type="网页", topics=["人工智能"]), "")

print("1. 重跑成功：只改机器字段，补上概览")
notion.find_materials = lambda st, limit=50: [page("p1", "原始标题", "https://e.com/1")]
text, _ = pipeline._handle_retry(lambda t: None)
check("调用了 refresh_machine_fields", len(refreshed) == 1)
kw = refreshed[0][1]
check("处理状态改成完成", kw["processing"] == "完成", str(kw))
check("标题保留来源原题", kw["title"] == "抓到的", str(kw))
check("概览是追加而不是覆盖", len(appended) == 1 and appended[0][0] == "p1")
check("没有动其他任何属性", other_updates == [], str(other_updates))
check("回复里报了成功数", "1 条成功" in text, text)

print("2. 查询范围包含两种未完成状态")
seen = []
notion.find_materials = lambda st, limit=50: seen.append(st) or []
pipeline._handle_retry(lambda t: None)
check("覆盖 需处理 和 等待采集", set(seen[0]) == {"需处理", "等待采集"}, str(seen))

print("3. 没有待重跑时不报错")
text, _ = pipeline._handle_retry(lambda t: None)
check("友好提示", "没有需要重跑" in text, text)

print("4. 抓不到正文：不标成完成")
refreshed.clear(); appended.clear()
extract.fetch_article = lambda u: {"ok": False, "error": "404", "title": "", "text": ""}
notion.find_materials = lambda st, limit=50: [page("p2", "抓不到的", "https://e.com/2")]
text, _ = pipeline._handle_retry(lambda t: None)
check("没有改成完成", refreshed == [], str(refreshed))
check("报告了失败", "1 条仍未完成" in text, text)

print("5. 没有原始链接：跳过而不是崩溃")
notion.find_materials = lambda st, limit=50: [page("p3", "无链接", "")]
text, _ = pipeline._handle_retry(lambda t: None)
check("安全跳过", "没有原始链接" in text, text)

print("6. 时间预算：不会跑到 Cloud Run 超时")
check("预算 < 300s 上限", pipeline.RETRY_BUDGET_SECONDS < 300, str(pipeline.RETRY_BUDGET_SECONDS))
extract.fetch_article = lambda u: (time.sleep(0.05), {"ok": True, "error": "", "title": "t", "text": "x"})[1]
notion.find_materials = lambda st, limit=50: [page(f"p{i}", f"t{i}", f"https://e.com/{i}") for i in range(4)]
pipeline.RETRY_BUDGET_SECONDS = 0.01
text, _ = pipeline._handle_retry(lambda t: None)
check("超预算后停下并告知还剩多少", "再发一次 /retry" in text, text)

print("\n" + ("PASS" if not fails else f"FAIL: {fails}"))
sys.exit(1 if fails else 0)
