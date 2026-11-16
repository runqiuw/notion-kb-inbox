"""在指定 Notion 页面下建好 bot 需要的两个数据库，打印两个 database id。

    NOTION_TOKEN=ntn_... python tools/setup_notion.py --parent <页面ID或链接>

前提：先在 Notion 建一个空页面，并在页面右上角 ··· → Connections 里加入你的
integration（否则 API 会返回 404）。

脚本会在该页面下新建「科研阅读与思考」页面，里面放「资料库」「想法与笔记」两个数据库，
属性名与选项逐字对应 docs/notion-schema.md —— 代码按中文属性名写入，改名会导致 400。

API 建不了的部分（视图、首页布局、「记下想法」按钮属性）见 docs/setup-notion.md 手工步骤。

加 --wiki 时，在同一父页面下再建「Wiki」页和「Wiki 条目」「维护记录」两个库（docs/wiki-layer.md）。
"""
import argparse
import os
import re
import sys

import httpx

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"


def opts(*names):
    return {"options": [{"name": n} for n in names]}


TOPICS = opts()  # 标签词表由 bot 写入时自动创建选项；初始可为空

MATERIAL_PROPS = {
    "标题": {"title": {}},
    "材料类型": {"select": opts("帖子", "网页", "论文", "视频", "播客", "聊天记录", "图片", "其他")},
    "来源平台": {"select": opts("小红书", "知乎", "B站", "YouTube", "微信", "网页", "论文", "本次对话", "演示")},
    "阅读状态": {"select": opts("待读", "阅读中", "已读", "搁置")},
    "处理状态": {"select": opts("完成", "需处理", "等待采集")},  # /retry 按这几个选项筛选，缺了会 400
    "主题": {"multi_select": TOPICS},
    "卡片摘要": {"rich_text": {}},
    "原始链接": {"url": {}},
    "作者": {"rich_text": {}},
    "发表时间": {"date": {}},
    "会议/期刊": {"rich_text": {}},
    "引用数": {"number": {"format": "number"}},
    "收集时间": {"created_time": {}},
    "缩略图": {"files": {}},
    "样例来源": {"select": opts("用户提供", "公开材料演示", "合成演示")},
}

THOUGHT_PROPS = {
    "标题": {"title": {}},
    "类型": {"select": opts("随想", "阅读笔记", "研究分析", "聊天提炼")},
    "笔记状态": {"select": opts("待展开", "整理中", "已形成结论")},
    "主题": {"multi_select": TOPICS},
    "创建时间": {"created_time": {}},
    "样例来源": {"select": opts("用户提供", "公开材料演示", "合成演示", "流程测试")},
}

# 纯展示用；某些 API 版本或工作区可能拒绝，失败只警告。
OPTIONAL_MATERIAL_PROPS = {
    "记录编号": {"unique_id": {"prefix": "SRC"}},
    "卡片信息": {"formula": {"expression":
                 'join([prop("材料类型"), prop("阅读状态")].filter(not empty(current)), " · ")'}},
}


def page_id(value: str) -> str:
    m = re.search(r"([0-9a-f]{32})(?:\?|$)", value.replace("-", ""))
    if not m:
        sys.exit(f"看不懂的页面 ID：{value}")
    return m.group(1)


class Notion:
    def __init__(self, token: str):
        self.http = httpx.Client(timeout=30, headers={
            "Authorization": f"Bearer {token}", "Notion-Version": VERSION})

    def call(self, method, path, body=None):
        r = self.http.request(method, API + path, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json()


def title(text):
    return [{"type": "text", "text": {"content": text}}]


def build(api: Notion, parent: str, root_title: str) -> tuple[str, str]:
    root = api.call("POST", "/pages", {
        "parent": {"page_id": parent},
        "properties": {"title": {"title": title(root_title)}},
        "icon": {"type": "emoji", "emoji": "📚"},
    })
    material = api.call("POST", "/databases", {
        "parent": {"page_id": root["id"]}, "title": title("资料库"),
        "is_inline": True, "properties": MATERIAL_PROPS})
    thought = api.call("POST", "/databases", {
        "parent": {"page_id": root["id"]}, "title": title("想法与笔记"),
        "is_inline": True, "properties": THOUGHT_PROPS})

    # 双向关联：笔记侧「关联资料」，资料侧 Notion 自动生成的反向字段改名为「关联笔记」。
    thought = api.call("PATCH", f"/databases/{thought['id']}", {"properties": {
        "关联资料": {"relation": {"database_id": material["id"], "dual_property": {}}}}})
    synced = thought["properties"]["关联资料"]["relation"]["dual_property"]["synced_property_name"]
    api.call("PATCH", f"/databases/{material['id']}", {"properties": {synced: {"name": "关联笔记"}}})
    api.call("PATCH", f"/databases/{thought['id']}", {"properties": {
        "资料标签": {"rollup": {"relation_property_name": "关联资料",
                                "rollup_property_name": "主题", "function": "show_unique"}}}})

    for name, spec in OPTIONAL_MATERIAL_PROPS.items():
        try:
            api.call("PATCH", f"/databases/{material['id']}", {"properties": {name: spec}})
        except RuntimeError as exc:
            print(f"  ! 跳过展示属性「{name}」，可在 Notion 里手工加：{exc}", file=sys.stderr)
    return material["id"].replace("-", ""), thought["id"].replace("-", "")


def one_way(db_id):
    return {"relation": {"database_id": db_id, "single_property": {}}}


def build_wiki(api: Notion, parent: str, material: str, thought: str) -> tuple[str, str]:
    """Wiki 层（可选）：总索引页 + Wiki 条目 + 维护记录，关联只在 Wiki 一侧。见 docs/wiki-layer.md。"""
    wiki = api.call("POST", "/pages", {
        "parent": {"page_id": parent},
        "properties": {"title": {"title": title("Wiki")}},
        "icon": {"type": "emoji", "emoji": "📖"},
        "children": [{"type": "paragraph", "paragraph": {"rich_text": title(
            "主题总索引：按主题列出每个条目的编号、链接、能回答的问题和检索别名。"
            "由 chatbot 按 wiki-skills/ 里的维护 skill 更新。")}}],
    })
    entries = api.call("POST", "/databases", {
        "parent": {"page_id": wiki["id"]}, "title": title("Wiki 条目"),
        "description": title("每条回答一个反复会问的问题；关系指向知识库，只在 Wiki 一侧。"),
        "properties": {
            "标题": {"title": {}},
            "编号": {"rich_text": {}},
            "一句话说明": {"rich_text": {}},
            "内容状态": {"select": opts("草稿", "可用", "待修订", "已归档")},
            "核查状态": {"select": opts("未核查", "部分核查", "已核查")},
            "最近核查": {"date": {}},
            "关联资料": one_way(material),
            "关联笔记": one_way(thought),
        }})
    api.call("PATCH", f"/databases/{entries['id']}", {"properties": {
        "相关 Wiki": one_way(entries["id"]), "合并后指向": one_way(entries["id"])}})
    log = api.call("POST", "/databases", {
        "parent": {"page_id": wiki["id"]}, "title": title("维护记录"),
        "properties": {
            "标题": {"title": {}},
            "日期": {"date": {}},
            "目标条目": one_way(entries["id"]),
            "结果": {"select": opts("完成", "部分完成", "待处理")},
        }})
    return entries["id"].replace("-", ""), log["id"].replace("-", "")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--parent", required=True, help="已加入 integration Connections 的页面 ID 或链接")
    p.add_argument("--title", default="科研阅读与思考", help="新建的根页面标题")
    p.add_argument("--wiki", action="store_true", help="同时在父页面下建 Wiki 层（见 docs/wiki-layer.md）")
    args = p.parse_args()
    token = os.environ.get("NOTION_TOKEN") or sys.exit("请先设置 NOTION_TOKEN")
    api, parent = Notion(token), page_id(args.parent)
    material, thought = build(api, parent, args.title)
    print("\n建好了。把下面两行填进 .env：")
    print(f"NOTION_MATERIAL_DB={material}")
    print(f"NOTION_THOUGHT_DB={thought}")
    if args.wiki:
        entries, log = build_wiki(api, parent, material, thought)
        print("\nWiki 层也建好了（skill 页需手工贴入 wiki-skills/ 的内容）。想一起备份就填：")
        print(f"NOTION_WIKI_DB={entries}")
        print(f"NOTION_WIKI_LOG_DB={log}")


if __name__ == "__main__":
    main()
