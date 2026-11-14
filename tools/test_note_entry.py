"""The 「记下想法」 entry: deep link, armed material, and who wins when both apply."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, notion, pipeline, store, telegram

PAGE_HEX = "0123456789abcdef0123456789abcdef"
PAGE_ID = "01234567-89ab-cdef-0123-456789abcdef"

# ---------- the link ----------
telegram._me_username = None
config.TELEGRAM_BOT_USERNAME = "kb_test_bot"
assert telegram.note_deeplink(PAGE_ID) == \
    f"https://t.me/kb_test_bot?start=n{PAGE_HEX}"
assert len(f"n{PAGE_HEX}") <= 64          # Telegram's deep-link payload ceiling

# No bot name means no entry at all, rather than a link into an empty chat.
telegram._me_username = None
config.TELEGRAM_BOT_USERNAME = ""
config.TELEGRAM_BOT_TOKEN = ""
assert telegram.note_deeplink(PAGE_ID) == ""

telegram._me_username = "kb_test_bot"
block = notion.link_callout("读完想说点什么？", "记下想法", "https://t.me/x?start=n1")
rt = block["callout"]["rich_text"]
assert rt[0]["text"]["content"] == "读完想说点什么？"
assert rt[1]["text"]["link"]["url"] == "https://t.me/x?start=n1"

# ---------- the entry is appended to every new material ----------
appended = []
notion.create_material = lambda **kw: {"id": PAGE_ID, "url": "https://notion.so/m"}
notion.append_blocks = lambda page_id, blocks: appended.append((page_id, blocks))
page = pipeline._create_material(title="资料", material_type="网页", platform="网页",
                                 url=None, topics=[], processing="完成", blocks=[])
assert page["id"] == PAGE_ID
assert appended[0][0] == PAGE_ID
tail = appended[0][1]
# The section heading comes first, so notes mirrored later land under it and
# do not read as one more paragraph of the material.
assert tail[0]["heading_2"]["rich_text"][0]["text"]["content"] == "我的想法"
assert tail[1]["callout"]["rich_text"][1]["text"]["link"]["url"] \
    .endswith(f"start=n{PAGE_HEX}")

# No bot name: the section still opens, just without the link.
telegram._me_username = ""
appended.clear()
pipeline._create_material(title="资料", material_type="网页", platform="网页",
                          url=None, topics=[], processing="完成", blocks=[])
assert len(appended[0][1]) == 1 and "heading_2" in appended[0][1][0]
telegram._me_username = "kb_test_bot"

# A material that saved is worth more than its entry: the append may fail.
def boom(*a, **k):
    raise RuntimeError("notion down")
notion.append_blocks = boom
assert pipeline._create_material(title="资料", material_type="网页", platform="网页",
                                 url=None, topics=[], processing="完成",
                                 blocks=[])["id"] == PAGE_ID

# ---------- tapping the link arms the next message ----------
pending = {}
def arm(chat_id, page_id):
    pending[chat_id] = page_id
    return 'a' * 32
store.set_pending_note = arm
store.take_pending_note = lambda chat_id: pending.pop(chat_id, None)
prompts = []
telegram.send = lambda *a, **kw: prompts.append((a, kw)) or {'message_id': 10}
notion.get_page = lambda page_id: {
    "id": page_id, "url": "https://notion.so/m",
    "properties": {"标题": {"type": "title", "title": [{"plain_text": "世界模型"}]}}}

msg = {"chat": {"id": 7}, "message_id": 1}
reply, url = pipeline._handle_note_start(msg, f"n{PAGE_HEX}")
assert reply == url == '' and pending[7] == PAGE_ID
assert "世界模型" in prompts[-1][0][1]
assert prompts[-1][1]['markup']['inline_keyboard'][-1] == [
    {'text': '取消', 'callback_data': 'nc:' + 'a' * 32}]

# Junk payloads and deleted materials answer, they do not raise.
assert pending.pop(7) and not pending
assert "不认识" in pipeline._handle_note_start(msg, "nNOTHEX")[0]
notion.get_page = lambda page_id: None
assert "已经不在" in pipeline._handle_note_start(msg, f"n{PAGE_HEX}")[0]
assert not pending

# ---------- the next message becomes a note on that material ----------
thoughts = []
notion.create_thought = lambda **kw: thoughts.append(kw) or {
    "id": "t", "url": "https://notion.so/t"}
notion.body_location = lambda page: (page["id"], [])
store.put_record = lambda *a, **k: "rid"
store.get_record = lambda chat_id, message_id: None

pending[7] = PAGE_ID
text, _ = pipeline._handle_thought(msg, "这篇讲的是可扩展训练")
assert thoughts[-1]["related"] == [PAGE_ID]
assert thoughts[-1]["note_type"] == "阅读笔记"
assert "关联" in text
assert not pending, "the arming is consumed once used"

# An explicit reply beats a stale arming -- but the arming still gets consumed.
pending[7] = PAGE_ID
store.get_record = lambda chat_id, message_id: {
    "notion_page_id": "other-material", "kind": "material"}
pipeline._handle_thought({"chat": {"id": 7}, "message_id": 2,
                          "reply_to_message": {"message_id": 1}}, "另一条想法")
assert thoughts[-1]["related"] == ["other-material"]
assert not pending

# Nothing armed, nothing replied to: still an ordinary standalone thought.
store.get_record = lambda chat_id, message_id: None
pipeline._handle_thought(msg, "独立的随想")
assert thoughts[-1]["related"] == [] and thoughts[-1]["note_type"] == "随想"

# ---------- routing ----------
# A bare /start is Telegram opening the chat; only a payload is a real action.
seen = []
pipeline._handle_note_start = lambda m, p: seen.append(p) or ("ok", "")
assert pipeline.dispatch({"chat": {"id": 7}, "message_id": 3,
                          "text": f"/start n{PAGE_HEX}"}, False)[0] == "ok"
assert seen == [f"n{PAGE_HEX}"]

print("PASS")
