"""The model decides whether the text beside a link is the user's own thought.

The keyword list can only catch templates someone remembered to add, and it
cannot tell that a sentence is just the article's own opening pasted back. The
model gets asked because it already has the article in hand -- but it only ever
decides keep-or-drop. What gets stored is the user's text, unchanged.
"""
import os
import sys
import types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, enrich, extract, notion, pipeline, storage, store, telegram

config.LLM_USE_SCHEMA = False
enrich.settings = types.SimpleNamespace(
    active=lambda: {"name": "x", "base_url": "http://x/v1", "model": "m", "api_key": "k"})
enrich.notion = types.SimpleNamespace(known_topics=lambda: ["人工智能"])

seen = {}


def _endpoint(note_is_yours=True):
    def fake(messages, use_schema, max_tokens=1500, **kwargs):
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[1]["content"]
        body = '{"title":"t","overview":"o","material_type":"网页","topics":[],"key_points":["要点"]'
        return body + (f',"note_is_yours":{str(note_is_yours).lower()}'
                       if note_is_yours is not None else "") + "}"
    return fake


# ---------- the question is only asked when there is text to ask about ----------
enrich._chat = _endpoint()
enrich.enrich("标题", "正文")
assert "note_is_yours" not in seen["system"], "没有附带文字时不该问这个问题"
assert "用户连同链接发来的文字" not in seen["user"]

enrich.enrich("标题", "正文", note="这篇说的其实是采样偏差")
assert "note_is_yours" in seen["system"], "有附带文字时必须问"
assert "这篇说的其实是采样偏差" in seen["user"]

# ---------- a model that ignores the field must not cost the user a thought ----------
enrich._chat = _endpoint(note_is_yours=None)
en, err = enrich.enrich("标题", "正文", note="随便一句")
assert err == "" and en.note_is_yours is True, "字段缺失时默认保留"

# ---------- the verdict reaches the pipeline ----------
# Only the calls that would touch the network are replaced. The block builders
# are pure, and running the real ones is the point -- a stub would not notice if
# the caption stopped reaching the page at all.
thoughts = []
notion.create_thought = lambda **kw: thoughts.append(kw) or {"id": "t", "url": "u"}
notion.upload_file = lambda *a, **k: None
notion.body_location = lambda p: (p["id"], [])
store.put_record = lambda *a, **k: "rid"
store.get_record = lambda *a: None
pipeline._create_material = lambda **kw: {"id": "m", "url": "u"}
storage.archive = lambda *a, **k: "gs://x"
telegram.download = lambda f: (b"%PDF-", "a.pdf")
extract.pdf_text = lambda d: {"ok": True, "title": "论文", "text": "正文" * 40,
                              "pages": 3, "error": ""}

BOILERPLATE = "7.88 gJk:/ 打开豆包App看看吧！【一个视频】"
doc = {"chat": {"id": 1}, "message_id": 2,
       "document": {"file_id": "f", "file_name": "a.pdf",
                    "mime_type": "application/pdf"},
       "caption": BOILERPLATE}

# This path used to file the caption as a thought with no filtering at all.
enrich._chat = _endpoint(note_is_yours=False)
pipeline._handle_document(doc, lambda _t: None)
assert not thoughts, f"模板文案不该建成想法: {thoughts}"

REAL = "这篇的实验设计有问题，对照组选得太窄"
doc["caption"] = REAL
enrich._chat = _endpoint(note_is_yours=True)
pipeline._handle_document(doc, lambda _t: None)
assert len(thoughts) == 1, "真实想法必须留下"
stored = "".join(t["text"]["content"]
                 for b in thoughts[0]["blocks"]
                 for t in b["paragraph"]["rich_text"])
assert stored == REAL, f"存的必须是用户原文，一字不改：{stored!r}"
assert thoughts[0]["title"] == REAL[:60]
assert thoughts[0]['related'] == [], '同一文件消息中的文字不应默认关联'

print("PASS")
