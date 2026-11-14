"""A paper is not a web page.

Scraping arxiv.org/abs fed arXivLabs, "Bibliographic and Citation Tools" and
the rest of the page furniture into 正文. The API hands over the abstract and
the bibliography and nothing else, so the page ends up worth reading.
"""
import os
import sys
import types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, enrich, extract, notion, pipeline, store, telegram
import staging_stub
STAGED = staging_stub.install(store)

config.LLM_USE_SCHEMA = False

# ---------- every shape of arXiv link, version suffix stripped ----------
# export.arxiv.org answers an id_list carrying a version with no entry at all,
# so the suffix has to come off before the API sees it.
cases = {
    "https://arxiv.org/abs/1803.10122": "1803.10122",
    "https://arxiv.org/abs/2310.06825v2": "2310.06825",
    "https://arxiv.org/pdf/2310.06825v2.pdf": "2310.06825",
    "https://arxiv.org/pdf/1706.03762": "1706.03762",
    "https://doi.org/10.48550/arXiv.1803.10122": "1803.10122",
    "https://arxiv.org/abs/math/0211159": "math/0211159",
    "https://example.com/abs/123": "",
    "https://karpathy.github.io/2015/05/21/rnn-effectiveness/": "",
}
for url, want in cases.items():
    assert extract.arxiv_id(url) == want, f"{url} -> {extract.arxiv_id(url)!r}"

# ---------- the page ----------
ABSTRACT = "We explore building generative neural network models of RL environments."
extract.fetch_arxiv = lambda pid: {
    "ok": True, "error": "", "title": "World Models", "text": ABSTRACT,
    "authors": ["David Ha", "Jürgen Schmidhuber"], "published": "2018-03-27",
    "doi": "10.5281/zenodo.1207631", "journal_ref": ""}
extract.semantic_scholar = lambda ident: {"venue": "", "citations": 2069, "abstract": ""}
enrich.enrich = lambda t, x, h="", images=None, note="", paper_abstract="": (
    enrich.Enrichment(title="世界模型", overview="论文提出世界模型。",
                      abstract_zh="我们探索构建强化学习环境的生成式神经网络模型。",
                      material_type="论文", topics=["人工智能"]), "")

created = {}
pipeline._create_material = lambda **kw: created.update(kw) or {"id": "p", "url": "u"}
store.put_record = lambda *a, **k: "rid"
store.index_url = lambda u, r: None
notion.create_thought = lambda **kw: {"id": "t", "url": "tu"}

URL = "https://arxiv.org/abs/1803.10122"
msg = {"chat": {"id": 1}, "message_id": 2}
text, _ = pipeline._handle_paper(msg, URL, "1803.10122", "", "", lambda _t: None)

assert created["material_type"] == "论文"
props = created["extra_props"]
assert props["作者"]["rich_text"][0]["text"]["content"] == "David Ha、Jürgen Schmidhuber"
assert props["发表时间"]["date"]["start"] == "2018-03-27"
assert props["引用数"]["number"] == 2069
assert "会议/期刊" not in props, "preprint 没有会议，该留空而不是填 arXiv.org"

blocks = created["blocks"]
assert blocks[0]["type"] == "heading_2"
assert not any(b["type"] == "bookmark" for b in blocks)

def headings(bs):
    return [t["text"]["content"] for b in bs if b["type"] == "heading_2"
            for t in b["heading_2"]["rich_text"]]
assert headings(blocks) == ["中文概览", "Abstract · 中英对照"], headings(blocks)

body = "".join(t["text"]["content"] for b in blocks
               if b["type"] == "paragraph" for t in b["paragraph"]["rich_text"])
assert ABSTRACT in body
for junk in ("arXivLabs", "Bibliographic", "Demos", "Current browse context"):
    assert junk not in body, f"arXiv 的 UI 文本不该出现: {junk}"

# 引用数 is a moving number, so the page says when it was read.
# 来源与定位 is a plain toggle, with its metadata nested inside.
src = "".join(
    t["text"]["content"]
    for b in blocks if b["type"] == "toggle"
    for c in b["toggle"].get("children", [])
    if c["type"] == "bulleted_list_item"
    for t in c["bulleted_list_item"]["rich_text"])
assert "引用数：2069" in src and "查得" in src, src
assert "DOI：10.5281" in src

# 阅读用时 needs no special case: card_metadata only counts 原始正文 /
# 提取正文 / OCR 原文 / 图片文字, and this page has none of them.
from app.reading import card_metadata
assert "阅读用时" not in card_metadata(blocks, "论文")

# ---------- Semantic Scholar is rate limited; that is the normal path ----------
extract.semantic_scholar = lambda ident: {}
created.clear()
pipeline._handle_paper(msg, URL, "1803.10122", "", "", lambda _t: None)
assert "引用数" not in created["extra_props"], "拿不到就留空，不报错"
assert created["extra_props"]["作者"], "arXiv 那部分照常写入"

# ---------- a paper arXiv does not know about still keeps its link ----------
extract.fetch_arxiv = lambda pid: {
    "ok": False, "error": "arXiv 上没有 9999.99999", "title": "", "text": "",
    "authors": [], "published": "", "doi": "", "journal_ref": ""}
created.clear()
text, _ = pipeline._handle_paper(msg, URL, "9999.99999", "", "", lambda _t: None)
assert not created, "没有书目信息就不建页"
assert store.get_staged(URL) and "没拿到" in text and "暂存区" in text

print("PASS")

# ---------- a DOI that is not on arXiv: Crossref carries the bibliography ----------
# Neither source is complete on its own. arXiv has the abstract but never a
# conference; Crossref has the conference and an unmetered citation count but
# publishers often deposit no abstract at all.
extract.fetch_crossref = lambda d: {
    "ok": True, "error": "", "title": "Optuna", "text": "",   # ACM deposits none
    "authors": ["Takuya Akiba", "Shotaro Sano"], "published": "2019-07-25",
    "doi": d, "journal_ref": "KDD '19: The 25th ACM SIGKDD Conference",
    "citations": 8282}
extract.semantic_scholar = lambda ident: {
    "venue": "Knowledge Discovery and Data Mining", "citations": 11626,
    "abstract": "Optuna is a next-generation hyperparameter optimization framework."}

DOI_URL = "https://doi.org/10.1145/3292500.3330701"
assert extract.doi_from_url(DOI_URL) == "10.1145/3292500.3330701"
assert extract.doi_from_url("https://doi.org/10.48550/arXiv.1803.10122") == "", \
    "arXiv 自己的 DOI 有更好的来源，不该走 Crossref"

created.clear()
pipeline._handle_paper(msg, DOI_URL, "", "10.1145/3292500.3330701", "",
                       lambda _t: None)
props = created["extra_props"]
assert props["会议/期刊"]["rich_text"][0]["text"]["content"].startswith("KDD '19"), \
    "会议名要用 Crossref 的 event，那是 arXiv 给不了的"
assert props["引用数"]["number"] == 8282, "Crossref 的引用数不限流，优先用它"

body = "".join(t["text"]["content"] for b in created["blocks"]
               if b["type"] == "paragraph" for t in b["paragraph"]["rich_text"])
assert "next-generation hyperparameter" in body, "Crossref 没摘要时由 S2 补上"

# Both sources dry: a bibliography card is not a complete paper page.
extract.semantic_scholar = lambda ident: {}
created.clear()
text, _ = pipeline._handle_paper(msg, DOI_URL, "", "10.1145/3292500.3330701", "",
                                 lambda _t: None)
assert not created, "没有摘要就不建页"
assert store.get_staged(DOI_URL) and "摘要" in text, text

print("PASS (含 DOI)")
