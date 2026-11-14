"""A note about a material is mirrored onto it, and stays editable afterwards.

The mirror moves a note's body inside a synced block. That is exactly the thing
an edited Telegram message rewrites, so most of this file is about the edit path
continuing to hit the real paragraphs rather than appending beside them.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, notion, pipeline, store

config.NOTION_TOKEN = "test"
config.NOTION_THOUGHT_DB = "thoughts"

MATERIAL = "material-page"
NOTE = "note-page"
SYNCED = "synced-block"
PARA = ["para-1", "para-2"]

calls = []


def fake(method, path, payload=None):
    calls.append((method, path, payload))
    if method == "POST" and path == "/pages":
        return {"id": NOTE, "url": "https://notion.so/note"}
    if method == "GET" and path.startswith(f"/blocks/{NOTE}/children"):
        kids = calls_children.get(NOTE, [])
        return {"results": kids}
    if method == "GET" and path.startswith(f"/blocks/{SYNCED}/children"):
        return {"results": [{"id": i, "type": "paragraph"} for i in PARA]}
    if method == "PATCH":
        return {"results": [{"id": "appended"}]}
    return {}


calls_children = {NOTE: [{"id": SYNCED, "type": "synced_block"}]}
notion._call = fake
notion.related_topics = lambda ids: []

# ---------- a note with a material: wrapped, and mirrored onto it ----------
body = notion.paragraphs("第一段\n第二段")
notion.create_thought(title="想法", note_type="阅读笔记", topics=["AI"],
                      related=[MATERIAL], blocks=body)

created = next(p for m, path, p in calls if m == "POST" and path == "/pages")
kids = created["children"]
assert len(kids) == 1 and kids[0]["type"] == "synced_block"
assert kids[0]["synced_block"]["synced_from"] is None
assert kids[0]["synced_block"]["children"] == body

mirror = next(p for m, path, p in calls
              if m == "PATCH" and path == f"/blocks/{MATERIAL}/children")
assert mirror["children"][0]["type"] == "divider"
assert mirror["children"][1]["synced_block"]["synced_from"] == {"block_id": SYNCED}

# ---------- a standalone 随想: no wrapper, no mirror ----------
calls.clear()
calls_children[NOTE] = [{"id": i, "type": "paragraph"} for i in PARA]
notion.create_thought(title="随想", note_type="随想", topics=[], related=[],
                      blocks=body)
created = next(p for m, path, p in calls if m == "POST" and path == "/pages")
assert created["children"] == body
assert not [1 for m, path, _ in calls if m == "PATCH" and "children" in path]

# ---------- a failed mirror does not lose the note ----------
real = notion.append_blocks
def flaky(target, blocks):
    if target == MATERIAL:
        raise RuntimeError("notion down")
    return real(target, blocks)
notion.append_blocks = flaky
calls_children[NOTE] = [{"id": SYNCED, "type": "synced_block"}]
assert notion.create_thought(title="想法", note_type="阅读笔记", topics=[],
                             related=[MATERIAL], blocks=body)["id"] == NOTE
notion.append_blocks = real

# ---------- body_location: the container is the synced block, not the page ----------
assert notion.body_location({"id": NOTE}) == (SYNCED, PARA)
calls_children[NOTE] = [{"id": i, "type": "paragraph"} for i in PARA]
assert notion.body_location({"id": NOTE}) == (NOTE, PARA)

# ---------- editing a mirrored note rewrites it in place ----------
targets = []
notion.replace_paragraphs = lambda parent, ids, text: targets.append(
    (parent, ids, text)) or ids
notion.update_properties = lambda page_id, props: None
saved = {}
store.put_record = lambda c, m, **f: saved.update(f) or f"{c}:{m}"

store.get_record = lambda c, m: {
    "notion_page_id": NOTE, "kind": "thought",
    "body_block_ids": PARA, "body_parent_id": SYNCED, "notion_url": "u"}
pipeline._handle_edit({"chat": {"id": 1}, "message_id": 1}, "改过的文字")
assert targets[-1][0] == SYNCED, "an edit must land inside the synced block"
assert targets[-1][1] == PARA
assert saved["body_parent_id"] == SYNCED

# Records written before mirroring exist and must keep working.
store.get_record = lambda c, m: {
    "notion_page_id": NOTE, "kind": "thought",
    "body_block_ids": PARA, "notion_url": "u"}
pipeline._handle_edit({"chat": {"id": 1}, "message_id": 1}, "老记录的改动")
assert targets[-1][0] == NOTE, "an old record's body is a child of the page"

# ---------- the wrapper is missing: skip the mirror, keep the note ----------
calls.clear()
calls_children[NOTE] = [{"id": i, "type": "paragraph"} for i in PARA]
assert notion.create_thought(title="想法", note_type="阅读笔记", topics=[],
                             related=[MATERIAL], blocks=body)["id"] == NOTE
assert not [1 for m, path, _ in calls
            if m == "PATCH" and path == f"/blocks/{MATERIAL}/children"], \
    "a reference must never point at a page id"

print("PASS")
