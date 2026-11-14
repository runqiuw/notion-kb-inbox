"""A message we cannot handle must say so, not disappear.

dispatch used to return ("", "") for anything that was not text, a photo, a
document or a link. main reads an empty reply as "an interactive command
already answered on its own" and deletes the 「收到」 ack -- so a voice note
produced a receipt that vanished a moment later, with nothing stored and no
explanation. Silent loss is the one failure mode this project does not accept.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import notion, pipeline, store

built = []
notion.create_material = lambda **kw: built.append(kw) or {"id": "m", "url": "u"}
notion.create_thought = lambda **kw: built.append(kw) or {"id": "t", "url": "u"}
notion.body_location = lambda p: (p["id"], ["b1"])
store.take_pending_note = lambda c: None
store.put_record = lambda c, m, **f: f"{c}:{m}"
store.get_record = lambda c, m: None

fails = []
def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {label}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(label)


def send(**content):
    msg = {"message_id": 1, "chat": {"id": 1}, "from": {"id": 1}}
    msg.update(content)
    return pipeline.dispatch(msg, False)


print("每种不支持的消息都要有回音")
cases = [
    ("语音", {"voice": {"file_id": "v", "duration": 3}}, "语音"),
    ("视频消息", {"video_note": {"file_id": "v"}}, "视频"),
    ("视频文件", {"video": {"file_id": "v"}}, "视频"),
    ("动图", {"animation": {"file_id": "a"}}, "动图"),
    ("音频", {"audio": {"file_id": "a"}}, "音频"),
    ("贴纸", {"sticker": {"file_id": "s"}}, "贴纸"),
    ("位置", {"location": {"latitude": 1, "longitude": 2}}, "位置"),
    ("没见过的类型", {"dice": {"value": 4}}, "不知道怎么处理"),
]
for label, content, expect in cases:
    text, url = send(**content)
    check(f"{label} 有回执", bool(text.strip()), repr(text))
    check(f"{label} 说清是什么", expect in text, repr(text))
    check(f"{label} 不返回 Notion 链接", url == "", repr(url))
    print(f"       → {text.splitlines()[0]}")

print("\n语音要指路，不只是拒绝")
voice, _ = send(voice={"file_id": "v", "duration": 3})
check("提到系统听写这条出路", "听写" in voice, repr(voice))

print("\n不该有任何东西被写进 Notion")
check("没有建页", built == [], str(built))

print("\n能处理的消息不受影响")
check("纯文字仍然走想法路径",
      pipeline.dispatch({"message_id": 2, "chat": {"id": 1}, "from": {"id": 1},
                         "text": "一条想法"}, False)[0].startswith("✅"))
check("建了那一页", len(built) == 1, str(built))

print("\n" + ("PASS" if not fails else f"FAIL: {fails}"))
sys.exit(1 if fails else 0)
