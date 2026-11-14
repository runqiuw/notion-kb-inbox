"""Exercise real writers with strict request validation and exact text readback."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import chat_notes, enrich, notion, pipeline, restore, storage, telegram
from urllib.parse import parse_qs, urlsplit
from test_link_thoughts import NotionMemory


class Memory(NotionMemory):
    """In-memory Notion with paginated block reads and a droppable append reply."""
    def __init__(self):
        super().__init__()
        self.lose_next_append = False
        self.batches = []

    def call(self, method, path, payload=None):
        if method == "GET" and path.startswith("/blocks/"):
            parent = path.split("/")[2]
            offset = int(parse_qs(urlsplit(path).query).get("start_cursor", [0])[0])
            all_ids = self.children.get(parent, [])
            ids = all_ids[offset:offset + 100]
            more = offset + 100 < len(all_ids)
            return {"results": [copy.deepcopy(self.blocks[i]) for i in ids],
                    "has_more": more, "next_cursor": str(offset + 100) if more else None}
        if method == "PATCH" and path.endswith("/children"):
            self.batches.append(len(payload["children"]))
            result = super().call(method, path, payload)
            if self.lose_next_append:
                self.lose_next_append = False
                raise RuntimeError("Notion response lost after append")
            return result
        return super().call(method, path, payload)


class StrictMemory(Memory):
    def __init__(self):
        super().__init__()
        self.requests = []

    def call(self, method, path, payload=None):
        if payload and "children" in payload:
            def validate(blocks, depth=1):
                assert len(blocks) <= 100
                assert not blocks or depth <= 2
                count = len(blocks)
                for b in blocks:
                    body = b[b["type"]]
                    rich = body.get("rich_text", [])
                    assert len(rich) <= 100
                    assert all(len(r.get("text", {}).get("content", "")) <= 2000 for r in rich)
                    count += validate(body.get("children", []), depth + 1)
                return count
            count = validate(payload["children"])
            assert count <= 1000
            size = len(json.dumps(payload).encode())
            assert size <= 500_000
            self.requests.append((count, size))
        return super().call(method, path, payload)


class SizeTests(unittest.TestCase):
    def setUp(self):
        self.api = StrictMemory()
        p = patch.object(notion, "_call", self.api.call)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(notion, "_create_tagged_page", lambda payload: self.api.call("POST", "/pages", payload))
        p.start()
        self.addCleanup(p.stop)

    def material(self, blocks):
        return notion.create_material(title="长资料", material_type="网页", platform="网页",
                                      url=None, topics=[], processing="完成", blocks=blocks)

    def paragraphs(self, parent):
        out = []
        for b in notion.list_children(parent):
            kind, body = b["type"], b[b["type"]]
            if kind in {"paragraph", "code"}:
                out.append("".join(r["text"]["content"] for r in body["rich_text"]))
            if b["id"] in self.api.children:
                out.extend(self.paragraphs(b["id"]))
        return out

    def test_thousands_of_folded_web_lines_preserved_in_safe_requests(self):
        raw = "\n".join(f"原始段落 {i}" for i in range(2305))
        blocks = pipeline._web_blocks("", enrich.Enrichment(title="t"), [], raw)
        before = copy.deepcopy(blocks)
        page = self.material(blocks)
        self.assertEqual("\n".join(self.paragraphs(page["id"])), raw)
        self.assertEqual(blocks, before)
        self.assertGreater(len(self.api.requests), 1)

    def test_utf8_size_with_few_blocks_and_large_page_properties(self):
        raw = "\n".join("汉字🙂" * 450 for _ in range(180))
        page = self.material([notion.toggle("长正文", notion.paragraphs(raw))])
        self.assertEqual("\n".join(self.paragraphs(page["id"])), raw)
        self.assertGreater(len(self.api.requests), 2)

    def test_nested_and_large_synced_body_uses_same_container(self):
        raw = "\n".join(f"想法 {i}" for i in range(230))
        page = notion.create_thought(title="t", note_type=None, topics=[], related=[],
                                      blocks=notion.paragraphs(raw), sync_body=True)
        top = notion.list_children(page["id"])
        self.assertEqual([b["type"] for b in top], ["synced_block"])
        self.assertEqual("\n".join(self.paragraphs(top[0]["id"])), raw)
        page = self.material([notion.toggle("outer", [notion.toggle("inner", notion.paragraphs("deep"))])])
        self.assertEqual(self.paragraphs(page["id"]), ["deep"])

    def test_single_huge_chat_message_and_code_are_exact_and_resumable(self):
        for language in (None, "plain text"):
            raw = "  内容🙂\n" * 35000 + "尾部  "
            block = chat_notes._text_block(raw, "code" if language else "paragraph",
                                            **({"language": language} if language else {}))
            block[block["type"]]["rich_text"][0]["annotations"] = {"bold": True}
            page = self.material([])
            chat_notes._append_prefix(page["id"], [block])
            self.assertEqual("".join(self.paragraphs(page["id"])), raw)
            calls = len(self.api.requests)
            chat_notes._append_prefix(page["id"], [block])
            self.assertEqual(len(self.api.requests), calls)
            self.assertTrue(notion.list_children(page["id"])[0][block["type"]]["rich_text"][0]["annotations"]["bold"])

    def test_lost_append_response_resumes_without_duplicate_content(self):
        page = self.material([])
        raw = "\n".join(f"段落 {i}" for i in range(1200))
        blocks = pipeline._folded_blocks("原文", notion.paragraphs(raw))
        self.api.lose_next_append = True
        with self.assertRaises(RuntimeError):
            chat_notes._append_prefix(page["id"], blocks)
        chat_notes._append_prefix(page["id"], blocks)
        self.assertEqual("\n".join(self.paragraphs(page["id"])), raw)

    def test_backup_restore_batches_large_shallow_blocks_by_bytes(self):
        raw = "中文🙂" * 4000
        blocks = [{"id": f"source-{i}", **chat_notes._text_block(raw)} for i in range(40)]
        page = self.material([])
        restorer = restore.Restorer.__new__(restore.Restorer)
        restorer.api, restorer.ids = self.api, {}
        restorer.clean = copy.deepcopy
        restorer.journal = lambda: None
        restorer.append(page["id"], blocks)
        self.assertEqual(self.paragraphs(page["id"]), [raw] * 40)
        self.assertEqual(len(restorer.ids), 40)
        calls = len(self.api.requests)
        restorer.append(page["id"], blocks)
        self.assertEqual(calls, len(self.api.requests))


class PDFTests(unittest.TestCase):
    def test_long_pdf_keeps_original_and_summary_without_extracted_text(self):
        raw = "正文\n" * 2000
        msg = {"chat": {"id": 1}, "message_id": 1,
               "document": {"file_id": "f", "file_name": "long.pdf", "mime_type": "application/octet-stream"}}
        with patch.object(telegram, "download", return_value=(b"%PDF", "long.pdf")), \
             patch.object(storage, "archive", return_value="gs://archive/original.pdf") as archive, \
             patch.object(pipeline.extract, "pdf_text", return_value={"ok": True, "title": "t", "text": raw, "pages": 80}), \
             patch.object(enrich, "enrich", return_value=(enrich.Enrichment(title="t", overview="总结", key_points=["要点"]), "")) as model, \
             patch.object(notion, "upload_file", return_value="upload") as upload, \
             patch.object(pipeline, "_create_material", return_value={"id": "p", "url": "u"}) as create:
            pipeline._handle_document(msg)
        self.assertEqual(archive.call_args.args[-1], "application/pdf")
        self.assertEqual(upload.call_args.args[-1], "application/pdf")
        blocks = create.call_args.kwargs["blocks"]
        self.assertIn("总结", str(blocks))
        self.assertIn("要点", str(blocks))
        self.assertIn("pdf", [b["type"] for b in blocks])
        self.assertNotIn("正文\\n正文", str(blocks))
        self.assertTrue(model.call_args.args[2].startswith(enrich.PDF_SUMMARY_HINT))

    def test_very_long_pdf_summary_scope_and_conclusion_are_preserved(self):
        raw = "开头" + "正文" * 100000 + "末尾结论"
        text, scope = enrich.pdf_summary_input(raw)
        self.assertLess(len(text), enrich.SUMMARY_INPUT_CHARS)
        self.assertIn("开头", text)
        self.assertIn("末尾结论", text)
        self.assertIn("抽样", scope)
        short = "短正文"
        self.assertEqual(enrich.pdf_summary_input(short), (short, "总结范围：全部可提取正文"))


if __name__ == "__main__":
    unittest.main()
