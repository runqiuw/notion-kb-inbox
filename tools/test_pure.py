"""Unit tests for the parts that need no network or cloud credentials."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import extract, notion, store

fails = []
def eq(label, got, want):
    if got != want:
        fails.append(f"{label}\n   got:  {got!r}\n   want: {want!r}")

# --- URL normalization / dedupe ---
eq("strip utm",
   store.normalize_url("https://WWW.Example.com/a/?utm_source=x&id=7"),
   "https://example.com/a?id=7")
eq("trailing slash",
   store.normalize_url("https://example.com/a/"),
   store.normalize_url("https://example.com/a"))
eq("fragment ignored",
   store.normalize_url("https://example.com/a#section"),
   "https://example.com/a")
eq("bilibili share junk",
   store.normalize_url("https://www.bilibili.com/video/BV1x?vd_source=abc&spm=1"),
   "https://bilibili.com/video/BV1x")
assert store.url_hash("https://example.com/a/") == store.url_hash("https://example.com/a")

# --- URL detection in mixed Chinese text ---
eq("url in chinese text",
   extract.find_urls("这篇不错 https://arxiv.org/abs/1706.03762 值得读"),
   ["https://arxiv.org/abs/1706.03762"])
eq("trailing chinese punct",
   extract.find_urls("看这个 https://example.com/a。"),
   ["https://example.com/a"])
eq("no url", extract.find_urls("只是一个想法"), [])

# --- platform routing ---
eq("xhs", extract.guess_platform("https://www.xiaohongshu.com/explore/abc"), "小红书")
eq("arxiv", extract.guess_platform("https://arxiv.org/abs/1"), "论文")
eq("b23", extract.guess_platform("https://b23.tv/xyz"), "B站")
eq("plain", extract.guess_platform("https://example.com"), "网页")
eq("zhihu blocks plain http", extract.plain_http_blocked("https://zhuanlan.zhihu.com/p/1"), True)
eq("xhs blocks plain http", extract.plain_http_blocked("https://xiaohongshu.com/x"), True)
eq("arxiv is fetchable", extract.plain_http_blocked("https://arxiv.org/abs/1"), False)

# --- Notion block chunking (2000-char hard limit) ---
long = "字" * 5000
blocks = notion.paragraphs(long)
assert all(len(b["paragraph"]["rich_text"][0]["text"]["content"]) <= 2000 for b in blocks), "chunk too long"
eq("chunk count", len(blocks), 3)
joined = "".join(b["paragraph"]["rich_text"][0]["text"]["content"] for b in blocks)
eq("no data loss in chunking", joined, long)

multi = notion.paragraphs("一\n\n二\n三")
eq("blank lines dropped", len(multi), 3)

# --- source title preservation ---
t = notion._title("x" * 500)
assert len(t["title"][0]["text"]["content"]) == 500

print("PASS" if not fails else "FAIL")
for f in fails:
    print(" -", f)
sys.exit(1 if fails else 0)
