"""Route every message shape through pipeline.dispatch() with Notion, Telegram,
Firestore, GCS and the LLM all stubbed. Checks routing + relation wiring, not I/O."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import pipeline, enrich, extract, notion, storage, store, telegram
import staging_stub
STAGED = staging_stub.install(store)

calls = {"materials": [], "thoughts": [], "relations": []}
_records, _urls = {}, {}

# --- stubs ---
def fake_material(**kw):
    calls["materials"].append(kw)
    pid = f"mat-{len(calls['materials'])}"
    return {"id": pid, "url": f"https://notion.so/{pid}"}
def fake_thought(**kw):
    calls["thoughts"].append(kw)
    if kw.get("related"): calls["relations"].append((f"th-{len(calls['thoughts'])}", kw["related"]))
    pid = f"th-{len(calls['thoughts'])}"
    return {"id": pid, "url": f"https://notion.so/{pid}"}
notion.create_material = fake_material
notion.create_thought = fake_thought
notion.body_location = lambda page: (page["id"], ["b1"])
notion.page_alive = lambda pid: True   # dedupe now verifies the page still exists
notion.upload_file = lambda *a, **k: "up-1"
storage.archive = lambda *a, **k: "gs://bucket/x"
storage.archive_text = lambda *a, **k: "gs://bucket/x.txt"
telegram.download = lambda fid: (b"\xff\xd8fake", "p.jpg")
extract.fetch_article = lambda url: {"ok": True, "error": "", "title": "抓到的标题", "text": "正文" * 50}
extract.ocr_image = lambda d: {"ok": True, "text": "图里的文字", "error": ""}
extract.pdf_text = lambda d: {"ok": True, "title": "论文标题", "text": "摘要" * 50, "pages": 8, "error": ""}
enrich.enrich = lambda t, x, h="", images=None, note="": (enrich.Enrichment(title=t or "整理标题", overview="概览。", material_type="网页", topics=["人工智能"]), "")
store.put_record = lambda c, m, **f: _records.setdefault(f"{c}:{m}", {}).update(f) or f"{c}:{m}"
store.get_record = lambda c, m: _records.get(f"{c}:{m}")
store.find_by_url = lambda u: _urls.get(store.normalize_url(u))
store.index_url = lambda u, rid: _urls.__setitem__(store.normalize_url(u), _records[rid])
store.take_pending_note = lambda c: None   # no 「记下想法」 link was tapped
store.pending_url_owner = lambda url: None   # 没有链接在等 LLM
store.waiting_for_llm = lambda: []

def msg(mid, **kw):
    d = {"message_id": mid, "chat": {"id": 99}, "from": {"id": 7}}
    d.update(kw); return d

fails = []
def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)

print("1. 纯文本 -> 独立 Thought")
t, u = pipeline.dispatch(msg(1, text="world model 更适合承担高层 prediction"), False)
check("建了 thought，无关联", len(calls["thoughts"]) == 1 and not calls["thoughts"][0]["related"])
check("类型是随想", calls["thoughts"][0]["note_type"] == "随想")

print("2. 链接 -> Material")
t, u = pipeline.dispatch(msg(2, text="https://example.com/post"), False)
check("建了 material", len(calls["materials"]) == 1)
check("没顺手建 thought", len(calls["thoughts"]) == 1)
check("处理状态=完成", calls["materials"][0]["processing"] == "完成")

print("3. Reply 那条资料 -> Thought 自动关联")
t, u = pipeline.dispatch(msg(3, text="这里的 assumption 有点强",
                             reply_to_message={"message_id": 2}), False)
check("关联到了 material", calls["thoughts"][-1]["related"] == ["mat-1"], str(calls["thoughts"][-1]["related"]))
check("类型变成阅读笔记", calls["thoughts"][-1]["note_type"] == "阅读笔记")

print("4. 重复链接（带 utm）-> 不重复建页")
n_before = len(calls["materials"])
t, u = pipeline.dispatch(msg(4, text="https://www.example.com/post/?utm_source=x"), False)
check("没有建第二个 material", len(calls["materials"]) == n_before)
check("回复是已保存过", "♻️" in t, t)

print("5. 重复链接 + 文字 -> 忽略附带文字")
n_th = len(calls["thoughts"])
t, u = pipeline.dispatch(msg(5, text="https://example.com/post 但我觉得结论反了"), False)
check("没建重复 material", len(calls["materials"]) == n_before)
check("不建附带 thought", len(calls["thoughts"]) == n_th)
check("回复复用原资料", "已保存过" in t)

print("6. 链接 + 一句话 -> 只建 Material")
t, u = pipeline.dispatch(msg(6, text="https://example.com/new 值得细读"), False)
check("建了新 material", len(calls["materials"]) == n_before + 1)
check("不建附带 thought", len(calls["thoughts"]) == n_th)

print("7. 小红书抓不到 -> 进暂存区，不假装抓到了")
n_mat = len(calls["materials"])
t, u = pipeline.dispatch(msg(7, text="https://www.xiaohongshu.com/explore/abc 这个排版思路可以借鉴到组会汇报"), False)
check("没有建空页", len(calls["materials"]) == n_mat)
row = store.get_staged("https://www.xiaohongshu.com/explore/abc")
check("进了暂存区", bool(row) and "暂存区" in t, t)
check("暂存不建附带想法", row and not row.get("thought_ids") and len(calls["thoughts"]) == n_th)

print("8. 图片 -> Material(图片) + OCR")
t, u = pipeline.dispatch(msg(8, photo=[{"file_id": "f1", "file_size": 100}]), False)
check("材料类型=图片", calls["materials"][-1]["material_type"] == "图片")

print("9. PDF -> Material")
t, u = pipeline.dispatch(msg(9, document={"file_id": "d1", "file_name": "paper.pdf",
                                          "mime_type": "application/pdf"}), False)
check("建了 material", calls["materials"][-1]["title"] == "论文标题")

print("10. 编辑已发文本 -> 改写而不是新建")
edits = {}
notion.replace_paragraphs = lambda pid, ids, text: edits.update({"page": pid, "text": text}) or ["b1"]
notion.update_properties = lambda pid, props: None
_records["99:1"]["notion_page_id"] = "th-1"; _records["99:1"]["kind"] = "thought"
_records["99:1"]["body_block_ids"] = ["b1"]
n_th = len(calls["thoughts"])
t, u = pipeline.dispatch(msg(1, text="world model 更适合承担高层 planning"), True)
check("没有新建 thought", len(calls["thoughts"]) == n_th)
check("改写了原页面", edits.get("page") == "th-1" and "planning" in edits.get("text", ""), str(edits))

print("\n" + ("PASS" if not fails else f"FAIL: {fails}"))
sys.exit(1 if fails else 0)
