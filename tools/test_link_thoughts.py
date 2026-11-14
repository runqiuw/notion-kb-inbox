"""Link messages ignore attached text; historical notes remain recoverable.

Uses real pipeline / Notion functions with in-memory Firestore and Notion HTTP
responses. Assertions inspect stored page blocks, not just mocked call counts.
"""
import copy
import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, enrich, extract, main, notion, pipeline, storage, store, telegram
from test_park_expiry import Database

URL = "https://example.org/article"
NOTE = "这个结论还需要进一步核实"
EDIT = "改正：这个结论只适用于特定条件"


class NotionMemory:
    def __init__(self):
        self.pages, self.blocks, self.children = {}, {}, {}
        self.counter = 0
        self.fail_mirror_after_write = False

    def add(self, parent, children):
        out = []
        for child in children:
            self.counter += 1
            block = copy.deepcopy(child)
            block["id"] = f"block-{self.counter}"
            kind = block["type"]
            inner = block[kind].pop("children", [])
            block["has_children"] = bool(inner)
            self.blocks[block["id"]] = block
            self.children.setdefault(parent, []).append(block["id"])
            if inner:
                self.add(block["id"], inner)
            out.append(copy.deepcopy(block))
        return out

    def call(self, method, path, payload=None):
        parts = path.split("?", 1)[0].strip("/").split("/")
        if method == "POST" and path == "/pages":
            pid = f"page-{len(self.pages) + 1}"
            page = {"id": pid, "url": f"https://notion/{pid}",
                    "properties": copy.deepcopy(payload["properties"]), "parent": payload["parent"]}
            self.pages[pid] = page
            self.add(pid, payload.get("children", []))
            return copy.deepcopy(page)
        if parts[0] == "pages":
            page = self.pages[parts[1]]
            if method == "PATCH":
                page["properties"].update(copy.deepcopy(payload["properties"]))
            return copy.deepcopy(page)
        bid = parts[1]
        if method == "GET":
            return {"results": [copy.deepcopy(self.blocks[k]) for k in self.children.get(bid, [])]}
        if method == "DELETE":
            for children in self.children.values():
                if bid in children:
                    children.remove(bid)
            return {}
        if parts[-1] == "children":
            results = self.add(bid, payload["children"])
            if self.fail_mirror_after_write and any(
                    c["type"] == "synced_block" and c["synced_block"].get("synced_from")
                    for c in payload["children"]):
                self.fail_mirror_after_write = False
                raise RuntimeError("Notion response lost after mirror was stored")
            return {"results": results}
        self.blocks[bid].update(copy.deepcopy(payload))
        return copy.deepcopy(self.blocks[bid])

    def text(self, parent):
        text = []
        for bid in self.children.get(parent, []):
            b = self.blocks[bid]
            kind, body = b["type"], b[b["type"]]
            if kind == "synced_block":
                source = (body.get("synced_from") or {}).get("block_id", bid)
                text.append(self.text(source))
            else:
                text.append("".join(r.get("text", {}).get("content", "") for r in body.get("rich_text", [])))
                text.append(self.text(bid))
        return "\n".join(text)

    def mirrors(self, page):
        return [self.blocks[bid]["synced_block"]["synced_from"]["block_id"]
                for bid in self.children.get(page, [])
                if self.blocks[bid]["type"] == "synced_block"
                and self.blocks[bid]["synced_block"].get("synced_from")]


class LinkThoughtTests(unittest.TestCase):
    def setUp(self):
        self.n = NotionMemory()
        self.db = Database()
        self.patch(store, "_db", self.db)
        self.patch(store.firestore, "transactional", lambda f: f)
        self.patch(notion, "_call", self.n.call)
        self.patch(notion, "related_topics", lambda _: [])
        self.patch(notion, "find_materials", lambda _: [])
        self.patch(config, "NOTION_MATERIAL_DB", "materials")
        self.patch(config, "NOTION_THOUGHT_DB", "thoughts")
        self.patch(telegram, "note_deeplink", lambda _: "")
        self.patch(telegram, "send", lambda *a, **kw: {})
        self.patch(storage, "archive_text", lambda *a: "gs://body")
        self.patch(extract, "semantic_scholar", lambda *a: {})
        self.fetch = self.patch(pipeline, "fetch_for", lambda *a: {
            "ok": True, "title": "真实资料", "text": "原始资料正文", "error": ""})
        self.patch(enrich, "enrich", lambda *a, **kw: (
            enrich.Enrichment(title="资料标题", overview="资料概览"), ""))

    def patch(self, obj, name, replacement):
        p = patch.object(obj, name, side_effect=replacement) if callable(replacement) else patch.object(obj, name, replacement)
        result = p.start()
        self.addCleanup(p.stop)
        return result

    def msg(self, mid=1, note=NOTE, url=URL):
        return {"chat": {"id": 7}, "message_id": mid, "text": url + " " + note}

    def pages(self, db):
        return [p for p in self.n.pages.values() if p["parent"]["database_id"] == db]

    def assert_note_edit(self, msg, material=None, linked=False):
        note = self.pages("thoughts")[0]
        material_before = copy.deepcopy(self.n.pages.get(material))
        blocks_before = copy.deepcopy([self.n.blocks[b] for b in self.n.children.get(material, [])])
        count = len(self.n.pages)
        reply, url = pipeline.dispatch({**msg, "text": EDIT}, True)
        self.assertIn("已更新", reply)
        self.assertEqual(url, note["url"])
        self.assertEqual(len(self.n.pages), count)
        self.assertIn(EDIT, self.n.text(note["id"]))
        self.assertNotIn(NOTE, self.n.text(note["id"]))
        title = note["properties"]["标题"]["title"][0]["text"]["content"]
        rec = store.get_record(7, msg['message_id']) or {}
        full = (rec.get('link_thought') or {}).get('preserve_full_message')
        expected = EDIT
        self.assertEqual(title, re.sub(r'\s+', ' ', expected).strip()[:200])
        if material:
            self.assertEqual(self.n.pages[material], material_before)
            self.assertEqual([self.n.blocks[b] for b in self.n.children[material]], blocks_before)
            if linked:
                self.assertIn(EDIT, self.n.text(material))
            else:
                self.assertNotIn(EDIT, self.n.text(material))
            self.assertNotIn(NOTE, self.n.text(material))

    def test_url_and_duplicate_ignore_attached_text(self):
        for mid in (1, 2):
            pipeline.dispatch(self.msg(mid=mid), False)
        self.assertEqual(len(self.pages('materials')), 1)
        self.assertEqual(self.pages('thoughts'), [])

    def test_markdown_link_ignores_label_and_attached_text(self):
        pipeline.dispatch(self.msg(url=f'[项目主页]({URL})'), False)
        self.assertEqual(store.get_record(7, 1)['url'], URL)
        self.assertEqual(self.pages('thoughts'), [])

    def test_staging_retry_and_caption_edit_never_create_thought(self):
        self.fetch.side_effect = lambda *a: {'ok': False, 'title': '', 'text': '', 'error': 'offline'}
        msg = self.msg()
        pipeline.dispatch(msg, False)
        pipeline.dispatch({**msg, 'text': URL + ' ' + EDIT}, True)
        self.assertEqual(self.pages('thoughts'), [])
        self.fetch.side_effect = lambda *a: {'ok': True, 'title': '资料', 'text': '完整正文', 'error': ''}
        pipeline._handle_retry(lambda _: None)
        self.assertIsNone(store.get_staged(URL))
        self.assertEqual(len(self.pages('materials')), 1)
        self.assertEqual(self.pages('thoughts'), [])

    def test_paper_url_does_not_create_attached_thought(self):
        self.patch(extract, 'fetch_arxiv', lambda _: {'ok': True, 'error': '', 'title': 'Paper',
            'text': 'The original abstract', 'authors': [], 'published': '', 'doi': '', 'journal_ref': ''})
        pipeline.dispatch(self.msg(url='https://arxiv.org/abs/1803.10122'), False)
        self.assertEqual(len(self.pages('materials')), 1)
        self.assertEqual(self.pages('thoughts'), [])

    def test_queued_duplicate_does_not_create_thought_or_unlock_owner(self):
        original = self.msg(note='')
        store.save_update(1, {'message': original})
        store.park_for_llm(1, URL)
        pipeline.dispatch(self.msg(mid=2), False, update_id=2)
        self.assertEqual(self.pages('thoughts'), [])
        self.assertEqual(store.pending_url_owner(URL), 1)
        pipeline.dispatch(original, False, update_id=1)
        self.assertEqual(len(self.pages('materials')), 1)
        self.assertEqual(self.pages('thoughts'), [])

    def test_legacy_note_is_not_overwritten_by_link_caption_edit(self):
        msg = self.msg()
        pipeline.dispatch(msg, False)
        page = notion.create_thought(title=NOTE, note_type='随想', topics=[], related=[], blocks=notion.paragraphs(NOTE))
        store.put_record(7, 1, link_thought={'id': page['id'], 'url': page['url'], 'text': NOTE})
        before = self.n.text(page['id'])
        reply, _ = pipeline.dispatch({**msg, 'text': URL + ' ' + EDIT}, True)
        self.assertIn('忽略', reply)
        self.assertEqual(self.n.text(page['id']), before)

    def test_legacy_conversion_retries_missing_tail_after_wrapper_write(self):
        note_text = "\n".join(f"第 {i} 条想法" for i in range(120))
        page = notion.create_thought(title="多段想法", note_type="随想", topics=[], related=[],
                                     blocks=notion.paragraphs(note_text))
        store.put_record(7, 1, link_thought={"id": page["id"], "url": page["url"]})
        store.stage_url(URL, 7, 1, "timeout", [page["id"]])
        store.update_chat_stage(URL, explicit_thought_ids=[page['id']])
        real_add = self.n.add
        def fail_tail(parent, children):
            # Legacy conversion has stored the first 100 paragraphs in a new
            # wrapper. Interrupt the following append before it is stored.
            if parent not in self.n.pages and self.n.children.get(parent) and all(
                    b["type"] == "paragraph" for b in children):
                raise RuntimeError("Notion failed appending the remaining paragraphs")
            return real_add(parent, children)
        self.n.add = fail_tail
        pipeline._handle_retry(lambda _: None)
        self.assertIsNotNone(store.get_staged(URL))
        self.n.add = real_add
        result, _ = pipeline._handle_retry(lambda _: None)
        self.assertIn("1 条成功", result)
        self.assertEqual(len(self.pages("materials")), 1)
        material = store.get_record(7, 1)["notion_page_id"]
        container, ids = notion.body_location(page)
        self.assertNotEqual(container, page["id"])
        self.assertEqual(len(ids), 120)
        self.assertEqual(self.n.text(material).count("第 119 条想法"), 1)

    def test_retry_after_lost_mirror_response_does_not_duplicate_note_or_reference(self):
        self.fetch.side_effect = lambda *a: {"ok": False, "title": "", "text": "", "error": "timeout"}
        pipeline.dispatch(self.msg(), False)
        legacy = notion.create_thought(title=NOTE, note_type='随想', topics=[], related=[], blocks=notion.paragraphs(NOTE))
        store.put_record(7, 1, link_thought={'id': legacy['id'], 'url': legacy['url']})
        store.stage_url(URL, 7, 1, 'timeout', [legacy['id']])
        store.update_chat_stage(URL, explicit_thought_ids=[legacy['id']])
        self.fetch.side_effect = lambda *a: {"ok": True, "title": "资料", "text": "完整正文", "error": ""}
        self.n.fail_mirror_after_write = True
        pipeline._handle_retry(lambda _: None)
        row = store.get_staged(URL)
        self.assertIsNotNone(row)
        self.assertTrue(row["thought_ids"])
        material = row["page_id"]
        self.assertEqual(len(self.n.mirrors(material)), 1)
        result, _ = pipeline._handle_retry(lambda _: None)
        self.assertIn("1 条成功", result)
        self.assertIsNone(store.get_staged(URL))
        self.assertEqual(len(self.pages("materials")), 1)
        self.assertEqual(len(self.pages("thoughts")), 1)
        self.assertEqual(len(self.n.mirrors(material)), 1)

    def test_legacy_staged_plain_note_gets_shared_body_without_removing_provenance(self):
        msg = self.msg()
        page = notion.create_thought(title=NOTE, note_type="随想", topics=[], related=[],
            blocks=[*notion.paragraphs(NOTE), notion.toggle_heading("来源与定位", notion.bullets([URL]))])
        store.put_record(7, 1, link_thought={"id": page["id"], "url": page["url"]})
        store.stage_url(URL, 7, 1, "timeout", [page["id"]])
        store.update_chat_stage(URL, explicit_thought_ids=[page['id']])
        result, _ = pipeline._handle_retry(lambda _: None)
        self.assertIn("1 条成功", result)
        material = store.get_record(7, 1)["notion_page_id"]
        self.assertEqual(len(self.n.mirrors(material)), 1)
        self.assertIn(URL, self.n.text(page["id"]))
        self.assert_note_edit(msg, material, linked=True)
        self.assertIn(URL, self.n.text(page["id"]))

    def test_editing_link_never_creates_or_erases_thoughts(self):
        msg = self.msg()
        pipeline.dispatch(msg, False)
        material = store.get_record(7, 1)['notion_page_id']
        before = copy.deepcopy(self.n.pages[material])
        pipeline.dispatch({**msg, 'text': URL}, True)
        self.assertEqual(self.n.pages[material], before)
        self.assertEqual(self.pages('thoughts'), [])


if __name__ == "__main__":
    unittest.main()
