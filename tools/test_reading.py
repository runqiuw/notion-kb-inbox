"""Reading cards, live tag vocabulary, inheritance and retry safety."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, enrich, notion, thumbnails
from app.reading import card_metadata

blocks = [notion.heading("中文概览"), *notion.paragraphs("一句话。第二句话。"),
          notion.heading("原始正文"), *notion.paragraphs("字" * 350),
          notion.toggle("展开全文", notion.paragraphs("word " * 200)),
          notion.heading("来源与定位"), *notion.paragraphs("noise " * 1000)]
assert card_metadata(blocks, "网页") == {"卡片摘要": "一句话。"}
assert "阅读用时" not in card_metadata(blocks, "播客")
assert "阅读用时" not in card_metadata(blocks, "视频")
assert "阅读用时" not in card_metadata(blocks[:2], "论文")
assert enrich._cap_topics([" RL ", "rl", "智能体", "新词", "又一新词", None],
                          ["RL", "智能体"]) == ["RL", "Agent"]

config.NOTION_TOKEN = "test"
config.NOTION_MATERIAL_DB = "materials"
config.NOTION_THOUGHT_DB = "thoughts"
notion._topics_cache.clear()
calls = []
def fake(method, path, payload=None):
    calls.append((method, path, payload))
    if path.startswith("/databases/"):
        tag = "智能体" if path.endswith("materials") else "科研方法"
        return {"properties": {"主题": {"multi_select": {"options": [{"name": tag}]}}}}
    if method == "GET":
        return {"properties": {"主题": {"multi_select": [{"name": "智能体"}]}}}
    return {"id": "created", "url": "https://notion.so/created"}
notion._call = fake
thumbnails.note_icon = lambda kind: {"type": "emoji", "emoji": thumbnails.EMOJI.get(kind, "📝")}
assert notion.known_topics() == ["智能体", "科研方法"]
assert notion.known_topics() == ["智能体", "科研方法"]
assert len(calls) == 2
notion.create_thought(title="原话", note_type="阅读笔记", topics=[], related=["material"], blocks=[])
assert calls[-1][2]["properties"]["主题"]["multi_select"] == [{"name": "Agent"}]
assert "阅读状态" not in calls[-1][2]["properties"]
notion.create_thought(title="原话", note_type="阅读笔记", topics=["我的标签"], related=["material"], blocks=[])
assert calls[-1][2]["properties"]["主题"]["multi_select"] == []
thumbnail = {"name": "缩略图", "type": "file_upload", "file_upload": {"id": "uploaded"}}
notion.create_material(title="资料", material_type="网页", platform="网页", url=None,
                       topics=[], processing="完成", blocks=blocks, thumbnail=thumbnail)
props = calls[-1][2]["properties"]
assert "cover" not in calls[-1][2]
assert props["缩略图"]["files"] == [thumbnail]
assert "阅读用时" not in props
assert props["阅读状态"]["select"]["name"] == "待读"
assert props["卡片摘要"]["rich_text"][0]["text"]["content"] == "一句话。"
notion.refresh_machine_fields("material", title="新标题", material_type="论文", topics=["AI 新猜测"], processing="完成")
assert "主题" not in calls[-1][2]["properties"]
assert "阅读状态" not in calls[-1][2]["properties"]
notion.refresh_machine_fields("material", title="新标题", material_type="论文", topics=[],
                              processing="完成", overview="重跑补上的概览。下一句。", existing_page={})
assert calls[-1][2]["properties"]["卡片摘要"]["rich_text"][0]["text"]["content"] == "重跑补上的概览。"
assert "阅读用时" not in calls[-1][2]["properties"]
notion.refresh_machine_fields("material", title="新标题", material_type="论文", topics=[],
                              processing="完成", overview="新的机器文字。", existing_page={
                                  "properties": {"卡片摘要": {"type": "rich_text", "rich_text": [{"plain_text": "我的摘要"}]}}})
assert "卡片摘要" not in calls[-1][2]["properties"]
def unavailable(*args, **kwargs):
    raise RuntimeError("offline")
notion._call = unavailable
assert notion.related_topics(["material"]) == []
print("PASS")
