"""在指定 Notion 页面下建好 bot 需要的两个数据库，打印两个 database id。

    NOTION_TOKEN=ntn_... python tools/setup_notion.py --parent <页面ID或链接>

前提：先在 Notion 建一个空页面，并在页面右上角 ··· → Connections 里加入你的
integration（否则 API 会返回 404）。

脚本会在该页面下新建「科研阅读与思考」页面，里面放「资料库」「想法与笔记」两个数据库，
属性名与选项逐字对应 docs/notion-schema.md —— 代码按中文属性名写入，改名会导致 400。

API 建不了的部分（视图、首页布局、「记下想法」按钮属性）见 docs/setup-notion.md 手工步骤。
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


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--parent", required=True, help="已加入 integration Connections 的页面 ID 或链接")
    p.add_argument("--title", default="科研阅读与思考", help="新建的根页面标题")
    args = p.parse_args()
    token = os.environ.get("NOTION_TOKEN") or sys.exit("请先设置 NOTION_TOKEN")
    material, thought = build(Notion(token), page_id(args.parent), args.title)
    print("\n建好了。把下面两行填进 .env：")
    print(f"NOTION_MATERIAL_DB={material}")
    print(f"NOTION_THOUGHT_DB={thought}")


if __name__ == "__main__":
    main()
