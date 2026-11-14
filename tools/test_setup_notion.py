"""setup_notion builds every property the bot writes, and wires the two-way relation."""
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import setup_notion as setup

ROOT = Path(__file__).resolve().parents[1]


class FakeNotion:
    def __init__(self):
        self.dbs, self.calls, self.n = {}, [], 0

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if path == "/pages":
            return {"id": "root"}
        if method == "POST" and path == "/databases":
            self.n += 1
            db = {"id": f"db{self.n}-x", "properties": dict(body["properties"])}
            self.dbs[db["id"]] = db
            return db
        db = self.dbs[path.split("/")[2]]
        for name, spec in body["properties"].items():
            if "name" in spec:
                db["properties"][spec["name"]] = db["properties"].pop(name)
                continue
            if "unique_id" in spec:
                raise RuntimeError("unsupported")
            if "relation" in spec:
                spec = {"relation": {**spec["relation"], "dual_property": {"synced_property_name": "Related to x"}}}
                self.dbs[spec["relation"]["database_id"]]["properties"]["Related to x"] = {"relation": {}}
            db["properties"][name] = spec
        return db


class SetupNotion(unittest.TestCase):
    def test_builds_schema_the_code_writes(self):
        api = FakeNotion()
        material, thought = setup.build(api, "parent", "科研阅读与思考")
        mprops = api.dbs["db1-x"]["properties"]
        tprops = api.dbs["db2-x"]["properties"]
        self.assertEqual((material, thought), ("db1x", "db2x"))
        self.assertIn("关联笔记", mprops)
        self.assertNotIn("Related to x", mprops)
        self.assertIn("资料标签", tprops)
        self.assertIn("卡片信息", mprops)
        self.assertNotIn("记录编号", mprops)  # optional failure is tolerated
        used = set(re.findall(r'"([一-鿿/]{2,6})"', "\n".join(
            p.read_text() for p in (ROOT / "app").glob("*.py"))))
        for name in ("标题", "材料类型", "来源平台", "阅读状态", "处理状态", "主题", "卡片摘要",
                     "原始链接", "作者", "发表时间", "会议/期刊", "引用数", "缩略图", "样例来源"):
            self.assertIn(name, used)
            self.assertIn(name, mprops)
        for name in ("类型", "笔记状态", "关联资料", "样例来源"):
            self.assertIn(name, tprops)

    def test_page_id_accepts_links(self):
        self.assertEqual(setup.page_id("https://www.notion.so/My-Page-0123456789abcdef0123456789abcdef?pvs=4"),
                         "0123456789abcdef0123456789abcdef")
        self.assertEqual(setup.page_id("01234567-89ab-cdef-0123-456789abcdef"),
                         "0123456789abcdef0123456789abcdef")


if __name__ == "__main__":
    unittest.main()
