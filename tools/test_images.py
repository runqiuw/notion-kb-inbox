"""Image-heavy posts: judge, transcribe the informative ones, keep every original.

小红书 图文帖 carry the substance in pictures. Cloud Vision would transcribe the
cover card and the watermark too; a vision model decides first. But the plan
(section 17) still forbids dropping an image because we took its text.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import enrich, extract, notion, pipeline, storage, store
pipeline.comments.capture = lambda *a: None
import staging_stub
STAGED = staging_stub.install(store)

fails = []
def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)

mats, archived = [], []
notion.create_material = lambda **kw: mats.append(kw) or {"id": "m", "url": "https://n/m"}
notion.create_thought = lambda **kw: {"id": "t", "url": "https://n/t"}
notion.body_location = lambda p: (p["id"], ["b"])
storage.archive = lambda data, name, ct=None: archived.append(name) or f"gs://b/{name}"
storage.archive_text = lambda *a, **k: "gs://b/t.txt"
store.put_record = lambda c, m, **f: f"{c}:{m}"
store.find_by_url = lambda u: None
store.index_url = lambda *a: None
store.pending_url_owner = lambda url: None   # 没有链接在等 LLM
store.waiting_for_llm = lambda: []
extract.plain_http_blocked = lambda u: True
extract.parsehub_platform = lambda u: "XhsParser"
extract.parse_via_parsehub = lambda u, c=None: {
    "ok": True, "title": "我为什么不做3D了", "text": "引言一句话。",
    "platform": "xhs", "images": [f"http://cdn/{i}.jpg" for i in range(4)], "error": ""}
extract.download_images = lambda urls: ([(b"img" + bytes([i]), "image/jpeg") for i in range(len(urls))], [])

passed_images = {}
def fake_enrich(t, x, h="", images=None, note=""):
    passed_images["n"] = len(images or [])
    return enrich.Enrichment(
        title="我为什么不做3D了", overview="作者复盘从3D转向的原因。",
        material_type="帖子", topics=["人工智能"], key_points=["作者认为原研究方向的发展空间有限。"],
        images=[
            enrich.ImageRead(index=0, informative=False, note="封面标题卡", text=""),
            enrich.ImageRead(index=1, informative=True, note="", summary="作者介绍了研究起点和时间。", text="我的研究生涯大概是在2022年左右从3D起步的。"),
            enrich.ImageRead(index=2, informative=True, note="", summary="作者解释转向的原因。", text="后来发现这个方向的天花板很低。"),
            enrich.ImageRead(index=3, informative=False, note="作者自拍", text=""),
        ]), ""
enrich.enrich = fake_enrich

pipeline.dispatch({"message_id": 1, "chat": {"id": 1}, "from": {"id": 1},
                   "text": "https://xhslink.cn/o/abc"}, False)
kw = mats[-1]
blocks = repr(kw["blocks"])

print("判断与转写")
check("图片传给了模型", passed_images["n"] == 4, str(passed_images))
check("有信息的图文字进了页面", "我的研究生涯大概是在2022年" in blocks)
check("第二张也进了", "后来发现这个方向的天花板很低" in blocks)
check("有「逐图总结」小节", "逐图总结" in blocks and "作者解释转向的原因。" in blocks)
check("标注了是机器转写", "机器转写" in blocks)
visible = repr([b for b in kw["blocks"] if b["type"] != "toggle"])
check("转写与帖子原文默认折叠", "我的研究生涯" not in visible and "引言一句话。" not in visible)
check("整体要点默认展示", "作者认为原研究方向的发展空间有限。" in visible)

print("装饰图：不转写，但要交代")
check("说明跳过了 2 张", "跳过了 2 张装饰性图片" in blocks, blocks[:200])
check("给了跳过理由", "封面标题卡" in blocks and "作者自拍" in blocks)

print("原件一张都不能少（计划第 17 条）")
check("4 张全部归档", len(archived) == 4, str(archived))
check("来源区记了归档数", "图片：4 张，已归档 4 张" in blocks, blocks[-400:])

print("下载有硬上限")
check("单帖图片数封顶", extract.MAX_IMAGES <= 9, str(extract.MAX_IMAGES))
check("总字节封顶", extract.MAX_TOTAL_IMAGE_BYTES <= 16 * 1024 * 1024)

print("没有图的材料不受影响")
mats.clear(); archived.clear()
extract.parse_via_parsehub = lambda u, c=None: {
    "ok": True, "title": "纯文字帖", "text": "正文" * 50, "platform": "xhs",
    "images": [], "error": ""}
enrich.enrich = lambda t, x, h="", images=None, note="": (
    enrich.Enrichment(title="纯文字帖", overview="概览"), "")
pipeline.dispatch({"message_id": 2, "chat": {"id": 1}, "from": {"id": 1},
                   "text": "https://xhslink.cn/o/def"}, False)
check("没有图片小节", "逐图总结" not in repr(mats[-1]["blocks"]))
check("没有多余归档", archived == [], str(archived))

print("格式嗅探：一张坏图不能拖垮整批")
check("jpeg 认得", extract.sniff_image_type(b"\xff\xd8\xff\xe0x") == "image/jpeg")
check("HEIC 必须先转换", extract.sniff_image_type(b"\x00\x00\x00$ftypheic") is None)
check("HTML 错误页拒收", extract.sniff_image_type(b"<html>") is None)

mats.clear(); archived.clear()
extract.parse_via_parsehub = lambda u, c=None: {
    "ok": True, "title": "混格式帖", "text": "引言。", "platform": "xhs",
    "images": [f"http://cdn/{i}" for i in range(3)], "error": ""}
extract.download_images = lambda urls: ([(b"good", "image/jpeg")],
                                        ["http://cdn/0", "http://cdn/2"])
enrich.enrich = lambda t, x, h="", images=None, note="": (
    enrich.Enrichment(title="混格式帖", overview="概览",
                      images=[enrich.ImageRead(index=0, informative=True, text="读到的文字")]), "")
pipeline.dispatch({"message_id": 9, "chat": {"id": 1}, "from": {"id": 1},
                   "text": "https://xhslink.cn/o/mix"}, False)
check("缺图不创建残缺页面", not mats)
check("缺图进入暂存重试", any("2 张图片未处理完整" in r["error"] for r in STAGED.values()), str(STAGED))
check("好图照常归档", len(archived) == 1, str(archived))

print("\n" + ("PASS" if not fails else f"FAIL: {fails}"))
sys.exit(1 if fails else 0)
