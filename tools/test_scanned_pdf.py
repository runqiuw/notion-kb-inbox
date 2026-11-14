"""A PDF with no text layer goes to the multimodal model, not to a new OCR.

The summarising call has to happen anyway, so rendered pages cost image tokens
and nothing else -- the same reasoning that put 图文帖 images through one call
instead of a dedicated OCR engine.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, enrich, extract, notion, pipeline, storage, store, telegram

config.LLM_USE_SCHEMA = False


def scan_like_pdf(pages: int) -> bytes:
    """A real PDF whose pages carry no extractable text -- drawn, not typed."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(200, 200)
    import io
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


raw = scan_like_pdf(14)

# pypdf finds nothing, which is exactly how a scan presents itself.
parsed = extract.pdf_text(raw)
assert parsed["ok"] is False and "扫描件" in parsed["error"], parsed

# Rendering is capped: a 200-page scan must not become 200 images.
imgs, total = extract.pdf_page_images(raw)
assert total == 14, total
assert len(imgs) == extract.MAX_SCAN_PAGES == 10, len(imgs)
assert all(b.startswith(b"\xff\xd8") for b, _ in imgs), "应当是 JPEG"

# ---------- the handler ----------
sent_images = {}


def fake_enrich(t, x, h="", images=None, note=""):
    sent_images["n"] = len(images or [])
    return enrich.Enrichment(
        title="扫描的论文", overview="这是一份扫描件。", material_type="论文",
        images=[enrich.ImageRead(index=i, informative=True, note="",
                                 text=f"第 {i + 1} 页的文字")
                for i in range(len(images or []))]), ""


enrich.enrich = fake_enrich
created = {}
pipeline._create_material = lambda **kw: created.update(kw) or {"id": "p", "url": "u"}
store.put_record = lambda *a, **k: "rid"
store.url_hash = lambda s: "h"
storage.archive = lambda *a, **k: "gs://x"
telegram.download = lambda f: (raw, "scan.pdf")
notion.upload_file = lambda *a, **k: None

msg = {"chat": {"id": 1}, "message_id": 2,
       "document": {"file_id": "f", "file_name": "scan.pdf",
                    "mime_type": "application/pdf"}}
pipeline._handle_document(msg, lambda _t: None)

assert sent_images["n"] == 10, f"只该送前 10 页: {sent_images}"
assert created["processing"] == "完成", "识别出文字就不该留在需处理"

body = "".join(t["text"]["content"] for b in created["blocks"]
               if b["type"] == "paragraph" for t in b["paragraph"]["rich_text"])
assert "这是一份扫描件。" in body
assert "第 1 页的文字" not in body and "第 10 页的文字" not in body

src = "".join(t["text"]["content"]
              for b in created["blocks"] if b["type"] == "toggle"
              for c in b["toggle"].get("children", [])
              if c["type"] == "bulleted_list_item"
              for t in c["bulleted_list_item"]["rich_text"])
assert "已识别前 10 页" in src and "共 14 页" in src, \
    f"页面必须说清只读了一部分: {src}"

# Reading estimates were removed from every material type.
from app.reading import card_metadata
assert "阅读用时" not in card_metadata(created["blocks"], "论文")

# ---------- a normal PDF keeps only its summary and original ----------
extract.pdf_text = lambda d: {"ok": True, "title": "有文字层", "text": "正文" * 60,
                              "pages": 3, "error": ""}
sent_images.clear()
created.clear()
pipeline._handle_document(msg, lambda _t: None)
assert sent_images["n"] == 0, "有文字层就不该渲染成图"
headings = [t["text"]["content"] for b in created["blocks"]
            if b["type"] == "heading_2" for t in b["heading_2"]["rich_text"]]
assert "提取正文" not in headings and "图片文字" not in headings, headings
assert "正文正文" not in str(created["blocks"])

print("PASS")
