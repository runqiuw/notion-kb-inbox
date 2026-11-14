"""Every dispatch reply must survive parse_mode="HTML".

main._process now sends the final reply as HTML, so any reply carrying a raw
'<' that is not one of Telegram's handful of allowed tags makes the send fail
with 400 and the user sees nothing at all. Literal help text is the easy way
to reintroduce that, which is what this file guards.
"""
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import pipeline, settings, telegram

# Telegram's HTML subset -- everything else is an "unsupported start tag".
ALLOWED = re.compile(r"</?(?:b|strong|i|em|u|s|code|pre|a(?: href=\"[^\"]*\")?)>")


def html_safe(text: str) -> bool:
    return "<" not in ALLOWED.sub("", text or "")


assert html_safe("普通文本") and html_safe("<b>粗体</b>")
assert html_safe('<a href="https://x">来源</a>')
assert not html_safe("用法：/llm use <名字>"), "the checker itself must catch this"

# ---------- the escaping helpers ----------
assert telegram.esc("a<b>&c") == "a&lt;b&gt;&amp;c"
assert telegram.bold("a<b") == "<b>a&lt;b</b>"
assert telegram.mono("1 < 2") == "<code>1 &lt; 2</code>"
assert telegram.link("来源", "https://x?a=1&b=2") == \
    '<a href="https://x?a=1&amp;b=2">来源</a>'
assert html_safe(telegram.bold("标题里有 <script>"))

# A title straight from a source page is the realistic case.
built = pipeline._success_text("✅ 已整理", "Attention <is> All You Need",
                               "概览 A & B", source_url="https://x?a=1&b=2",
                               platform="论文")
assert html_safe(built), built
assert "&lt;is&gt;" in built and "A &amp; B" in built

# ---------- literal replies ----------
assert html_safe(pipeline.HELP)
assert html_safe(pipeline.LLM_HELP)
assert html_safe(pipeline.llm_status_text())

# /llm usage paths. Some subcommands answer with a picker instead of text and
# would otherwise reach the network, so the send side is stubbed out; only the
# reply that dispatch returns has to be HTML-safe, because only that one is
# sent with parse_mode.
telegram.send = lambda *a, **k: {"message_id": 1}
telegram.edit = lambda *a, **k: {"message_id": 1}
settings.list_models = lambda: ([], "端点没给列表")
settings.save_picks = lambda *a, **k: None

for args in (["use"], ["add"], ["rm"], ["model"], ["没这个子命令"], []):
    text, _ = pipeline._handle_llm(args, lambda _t: None)
    assert html_safe(text), (args, text)

# The four that do answer with typed-argument syntax must actually say so --
# otherwise this file would pass by checking empty strings.
for args in (["use"], ["add"], ["rm"]):
    text, _ = pipeline._handle_llm(args, lambda _t: None)
    assert "用法" in text and "「" in text, (args, text)

print("PASS")
