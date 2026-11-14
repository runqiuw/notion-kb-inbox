"""公众号 is read from its own markup, not by generic heuristics.

WeChat leaves <title> empty for JS to fill and hides the body behind
`visibility: hidden`, so trafilatura returns a partial answer -- measured on a
real article: 1717 characters against 2060 from #js_content, and no title at all.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import extract

assert extract.guess_platform("https://mp.weixin.qq.com/s/abc") == "微信"

# Nested divs: the first </div> is never the end of an article body.
html = ('<div id="js_content" style="visibility: hidden;">'
        '<p>第一段</p><div><p>嵌套里的第二段</p></div><p>第三段</p>'
        '</div><div class="rich_media_tool">工具栏不该被算进去</div>')
body = extract._tags_to_text(extract._inner_html(html, 'id="js_content"'))
assert body == "第一段\n嵌套里的第二段\n第三段", repr(body)

assert extract._tags_to_text("a&nbsp;b &amp; c<br>d") == "a b & c\nd"
assert extract._tags_to_text('<script>var x=1</script><p>正文</p>') == "正文"
assert extract._inner_html("<div id='other'>x</div>", 'id="js_content"') == ""

# ---------- the three outcomes ----------
import httpx


class _Resp:
    def __init__(self, text): self.text = text
    def raise_for_status(self): pass


page = ('<html><head><title></title></head><body>'
        '<script>var msg_title = "马刺西决";</script>'
        '<div id="js_content"><p>正文第一段</p><p>正文第二段</p></div>'
        '</body></html>')
extract.httpx = type("m", (), {"get": staticmethod(lambda *a, **k: _Resp(page))})
got = extract.fetch_wechat("https://mp.weixin.qq.com/s/x")
assert got["ok"] and got["title"] == "马刺西决", got
assert got["text"] == "正文第一段\n正文第二段", repr(got["text"])

# A verification interstitial and a missing article need different answers, so
# the error has to say which one happened.
extract.httpx = type("m", (), {"get": staticmethod(
    lambda *a, **k: _Resp("<html>环境异常，请点击去验证</html>"))})
assert "要求验证" in extract.fetch_wechat("https://mp.weixin.qq.com/s/x")["error"]

extract.httpx = type("m", (), {"get": staticmethod(
    lambda *a, **k: _Resp("<html><body>别的什么页面</body></html>"))})
err = extract.fetch_wechat("https://mp.weixin.qq.com/s/x")["error"]
assert "没有 js_content" in err and "字节" in err, err

extract.httpx = httpx

# ---------- both entry points must take the same route ----------
# The 公众号 branch originally lived in the ingestion path only, so /retry kept
# sending those links through the generic extractor -- failing in exactly the
# way the branch existed to prevent. One router now, used by both.
from app import pipeline

routed = []
extract.fetch_wechat = lambda u: routed.append(("wechat", u)) or {
    "ok": True, "error": "", "title": "标题", "text": "正文"}
extract.fetch_article = lambda u: routed.append(("generic", u)) or {
    "ok": True, "error": "", "title": "t", "text": "x"}

WX = "https://mp.weixin.qq.com/s/x"
assert pipeline.fetch_for(WX)["title"] == "标题"
assert routed == [("wechat", WX)], routed

routed.clear()
pipeline.fetch_for("https://example.com/a")
assert routed == [("generic", "https://example.com/a")], routed

# /retry goes through the same router, not its own copy.
routed.clear()
pipeline.notion = type("n", (), {
    "prop_text": staticmethod(lambda p, k: WX if k == "原始链接" else "标题")})()
try:
    pipeline._retry_one({"id": "p"}, lambda _t: None)
except Exception:
    pass          # it goes on to enrich/Notion; the routing already happened
assert routed and routed[0][0] == "wechat", f"/retry 也必须走微信分支: {routed}"

print("PASS")
