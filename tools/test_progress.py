"""A slow ingestion must narrate itself, not go silent.

The user could not tell "still working" from "hung" while a link was being
fetched and summarised, so every slow stage now reports progress.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import enrich, extract, notion, pipeline, storage, store, telegram
import staging_stub
STAGED = staging_stub.install(store)

notes = []
notion.create_material = lambda **kw: {"id": "m1", "url": "https://notion.so/m1"}
notion.create_thought = lambda **kw: {"id": "t1", "url": "https://notion.so/t1"}
notion.body_location = lambda p: (p["id"], ["b1"])
notion.upload_file = lambda *a, **k: None
storage.archive = storage.archive_text = lambda *a, **k: ""
store.put_record = lambda c, m, **f: f"{c}:{m}"
store.get_record = lambda c, m: None
store.find_by_url = lambda u: None
store.index_url = lambda u, r: None
store.take_pending_note = lambda c: None   # no 「记下想法」 link was tapped
store.pending_url_owner = lambda url: None   # 没有链接在等 LLM
store.waiting_for_llm = lambda: []
extract.fetch_article = lambda u: {"ok": True, "error": "", "title": "T", "text": "正文" * 40}
extract.ocr_image = lambda d: {"ok": True, "text": "文字", "error": ""}
telegram.download = lambda f: (b"x", "p.jpg")
enrich.enrich = lambda t, x, h="", images=None, note="": (enrich.Enrichment(title="标题", overview="概览"), "")

msg = {"message_id": 1, "chat": {"id": 1}, "from": {"id": 1},
       "text": "https://example.com/article"}
pipeline.dispatch(msg, False, notes.append)

fails = []
def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)

print("链接：每个慢阶段都要有提示")
check("至少 3 条进度消息", len(notes) >= 3, str(notes))
check("抓取阶段有提示", any("抓取" in n for n in notes), str(notes))
check("整理阶段有提示", any("整理" in n for n in notes), str(notes))
check("写入阶段有提示", any("Notion" in n for n in notes), str(notes))
for n in notes:
    print(f"       → {n}")

notes.clear()
pipeline.dispatch({"message_id": 2, "chat": {"id": 1}, "from": {"id": 1},
                   "photo": [{"file_id": "f", "file_size": 1}]}, False, notes.append)
print("图片：下载 / OCR / 整理")
check("图片流程有进度", len(notes) >= 3, str(notes))
for n in notes:
    print(f"       → {n}")

notes.clear()
pipeline.dispatch({"message_id": 3, "chat": {"id": 1}, "from": {"id": 1},
                   "text": "一条纯想法"}, False, notes.append)
print("纯文本：本来就快，不该刷屏")
check("没有多余进度消息", notes == [], str(notes))

check("say 可省略（默认无操作）",
      pipeline.dispatch({"message_id": 4, "chat": {"id": 1}, "from": {"id": 1},
                         "text": "另一条想法"}, False) is not None)

print("\n" + ("PASS" if not fails else f"FAIL: {fails}"))
sys.exit(1 if fails else 0)
