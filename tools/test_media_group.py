"""A Telegram album must become one page, not one page per photo.

README offers "screenshot the article and send it" as the way round 公众号
being unreachable from the server. That only works if five screenshots of one
article land as one material: before this, each photo was its own update, its
own OCR and its own overview of one fifth of the argument.

Telegram never says which photo is the last one, so the first update to arrive
claims the group, waits, and builds the page for all of them.
"""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx
from google.api_core import exceptions as gexc

from app import config, enrich, main, notion, pipeline, storage, store, tasks, telegram

fails = []
def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)


# ---------- 1. leader election ----------
# The claim is the part that cannot be got wrong: album members arrive within
# milliseconds, so a read-then-write would let two of them both think they were
# first -- two pages, which is the bug being fixed.

class FakeRef:
    def __init__(self, store_): self.store = store_
    def create(self, data):
        if self.store.get("doc") is not None:
            raise gexc.AlreadyExists("already there")
        self.store["doc"] = dict(data)
    def get(self):
        doc = self.store.get("doc")
        return type("Snap", (), {"exists": doc is not None,
                                 "to_dict": lambda s, d=doc: d})()
    def set(self, data, **kw):
        self.store["doc"] = {**(self.store.get("doc") or {}), **data}
    def update(self, fields):
        for k, v in fields.items():
            vals = getattr(v, "values", None)
            if vals is not None:
                self.store["doc"][k] = self.store["doc"].get(k, []) + list(vals)
            else:
                self.store["doc"][k] = v

fake = {}
store.db = lambda: type("D", (), {
    "collection": lambda *a: type("C", (), {
        "document": lambda *a: FakeRef(fake)})()})()

def join(update_id, message_id, caption=""):
    return store.join_media_group(1, "G1", update_id, message_id,
                                  f"file{message_id}", caption)

print("抢占：只有第一条能当 leader")
roles = [join(100, 10, "五张截图"), join(101, 11), join(102, 12)]
check("第一条是 leader", roles[0] == "leader", str(roles))
check("其余都是 member", roles[1:] == ["member", "member"], str(roles))
check("三张都记进了组", len(fake["doc"]["members"]) == 3, str(fake["doc"]["members"]))

print("\n迟到的那张不跟着重建整组")
fake["doc"]["created_at"] = time.time() - store.MEDIA_GROUP_LATE_SECONDS - 1
check("超过窗口判为 late", join(103, 13) == "late")
check("late 不写进成员表", len(fake["doc"]["members"]) == 3,
      str(len(fake["doc"]["members"])))

print("\n成员顺序按 message_id，不是到达顺序")
fake["doc"]["members"] = [{"message_id": 12, "file_id": "c"},
                          {"message_id": 10, "file_id": "a"},
                          {"message_id": 11, "file_id": "b"}]
got = [m["file_id"] for m in store.media_group_members(1, "G1")]
check("按发送顺序排好", got == ["a", "b", "c"], str(got))


# ---------- 2. one page for the whole album ----------

built, records, uploads = [], [], []
notion.create_material = lambda **kw: built.append(kw) or {"id": "m1", "url": "https://notion.so/m1"}
notion.append_blocks = lambda *a, **k: None
notion.upload_file = lambda data, name, mime: uploads.append(name) or f"up{len(uploads)}"
telegram.note_deeplink = lambda pid: ""
telegram.download = lambda fid: (b"\xff\xd8\xff" + b"x" * 100, "p.jpg")
storage.archive = lambda *a, **k: "gs://bucket/x"
store.put_record = lambda c, m, **f: records.append(m) or f"{c}:{m}"

seen_images = []
def fake_enrich(title, text, hint="", images=None, note=""):
    seen_images.append(len(images or []))
    return enrich.Enrichment(
        title="公众号长文", overview="五张截图拼起来讲的是同一件事",
        material_type="图片", topics=["知识管理"],
        images=[enrich.ImageRead(index=0, informative=True, text="第一段正文"),
                enrich.ImageRead(index=1, informative=True, text="第二段正文"),
                enrich.ImageRead(index=2, informative=False, text="", note="页脚二维码")],
    ), ""
enrich.enrich = fake_enrich

members = [{"update_id": 100 + i, "message_id": 10 + i, "file_id": f"f{i}",
            "caption": "五张截图" if i == 0 else ""} for i in range(3)]
msg = {"message_id": 10, "chat": {"id": 1}, "from": {"id": 1},
       "media_group_id": "G1", "photo": [{"file_id": "f0", "file_size": 9}]}

notes = []
text, url = pipeline._handle_photo_group(msg, members, notes.append)

print("\n三张图 → 一页")
check("只建了一个页面", len(built) == 1, f"建了 {len(built)} 个")
check("三张图一次交给模型", seen_images == [3], str(seen_images))
check("三张原图都上传了", len(uploads) == 3, str(uploads))
check("回执说明合并了", "3 张" in text, repr(text))
check("材料类型是图片", built[0]["material_type"] == "图片", str(built[0].get("material_type")))

def block_text(bs):
    """All the text in a block tree. Toggles hold their contents in `children`,
    and both the skipped-image list and 来源与定位 live inside one."""
    out = []
    for b in bs:
        for v in b.values():
            if not isinstance(v, dict):
                continue
            for rt in (v.get("rich_text") or []):
                out.append(rt.get("text", {}).get("content", ""))
            if v.get("children"):
                out.append(block_text(v["children"]))
    return "\n".join(out)

body = block_text(built[0]["blocks"])
check("有「图 1」分节", "图 1" in body, body[:200])
check("有「图 2」分节", "图 2" in body, body[:200])
check("转写内容在页面里", "第一段正文" in body and "第二段正文" in body)
check("装饰图写明跳过理由", "页脚二维码" in body, body[:400])
check("来源里报了张数", "3 张" in body, body[:400])

print("\n每条消息都指向同一页，回复任意一张都能接上想法")
check("三条消息都写了 record", sorted(records) == [10, 11, 12], str(records))


# ---------- 3. routing ----------

single = []
pipeline._handle_photo = lambda m, say=None: single.append(m["message_id"]) or ("单张", "u")
store.media_group_members = lambda c, g: members

print("\n分流")
built.clear(); records.clear(); uploads.clear(); seen_images.clear()
pipeline.dispatch(dict(msg), False)
check("相册成员走合并路径", single == [], str(single))

straggler = dict(msg, message_id=99)
pipeline.dispatch(straggler, False)
check("迟到的那张走单张路径", single == [99], str(single))

store.media_group_members = lambda c, g: [members[0]]
pipeline.dispatch(dict(msg), False)
check("只剩一张时退回单张路径", single == [99, 10], str(single))

plain = {"message_id": 7, "chat": {"id": 1}, "from": {"id": 1},
         "photo": [{"file_id": "p", "file_size": 1}]}
pipeline.dispatch(plain, False)
check("普通单图不受影响", single == [99, 10, 7], str(single))


# ---------- 4. the webhook answers once, not once per photo ----------

fake.clear()
config.TELEGRAM_WEBHOOK_SECRET = ""
config.TELEGRAM_ALLOWED_USER_ID = ""
store.save_update = lambda uid, p: True
store.set_update_status = lambda *a, **k: None

acks, queued = [], []
telegram.send = lambda chat, text, **kw: acks.append(text) or {"message_id": 900 + len(acks)}
tasks.enqueue_process = lambda uid, redispatch=0, delay_seconds=0: (
    queued.append((uid, delay_seconds)) or True)

async def send_album():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        for i in range(3):
            await c.post("/telegram/webhook", json={
                "update_id": 500 + i,
                "message": {"message_id": 20 + i, "chat": {"id": 1},
                            "from": {"id": 1}, "media_group_id": "G9",
                            "photo": [{"file_id": f"g{i}", "file_size": 10}]}},
                timeout=30)
asyncio.run(send_album())

print("\n五张截图不该有五条回执")
check("只回了一句「收到」", len(acks) == 1, str(acks))
check("只入队一次", len(queued) == 1, str(queued))
check("入队带了收集延迟", queued and queued[0][1] == store.MEDIA_GROUP_DELAY_SECONDS,
      str(queued))
check("入队的是 leader 那条", queued and queued[0][0] == 500, str(queued))

acks.clear(); queued.clear()
async def send_single():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        await c.post("/telegram/webhook", json={
            "update_id": 600,
            "message": {"message_id": 30, "chat": {"id": 1}, "from": {"id": 1},
                        "photo": [{"file_id": "solo", "file_size": 10}]}},
            timeout=30)
asyncio.run(send_single())

print("\n单张照片一切照旧")
check("照常回执", len(acks) == 1, str(acks))
check("照常立刻入队，无延迟", queued == [(600, 0)], str(queued))

print("\n" + ("PASS" if not fails else f"FAIL: {fails}"))
sys.exit(1 if fails else 0)
