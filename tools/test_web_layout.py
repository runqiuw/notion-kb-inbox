"""Web reading cards retain source titles and archive text without repeating it."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import enrich, notion, pipeline, storage, store
import staging_stub


class WebLayout(unittest.TestCase):
    def setUp(self):
        mock = patch.object(pipeline.comments, "capture", return_value=None)
        mock.start(); self.addCleanup(mock.stop)

    def test_each_image_summary_is_visible_and_all_originals_are_folded(self):
        en = enrich.Enrichment(title="帖子", overview="整体概览。", key_points=["整体要点。"], images=[
            enrich.ImageRead(index=2, informative=True, summary="图表显示后期上升。", text=""),
            enrich.ImageRead(index=0, informative=False, note="封面"),
            enrich.ImageRead(index=1, informative=True, summary="图片的总结。", text="错乱 OCR 原文", note="金额模糊，无法辨认"),
        ])
        blocks = pipeline._web_blocks("https://xhslink.cn/a", en, ["来源"], "帖子原文", 3)
        visible = str([b for b in blocks if b["type"] != "toggle"])
        self.assertIn("图片的总结。", visible)
        self.assertIn("图表显示后期上升。", visible)
        self.assertIn("金额模糊，无法辨认", visible)
        self.assertLess(visible.index("图 2"), visible.index("图 3"))
        self.assertNotIn("错乱 OCR 原文", visible)
        self.assertNotIn("帖子原文", visible)
        self.assertIn("错乱 OCR 原文", str(blocks))
        self.assertIn("帖子原文", str(blocks))
        self.assertEqual(notion.card_metadata(blocks, "帖子")["卡片摘要"], "整体概览。")

    def test_long_originals_are_complete_across_multiple_toggles(self):
        raw = "\n".join(f"原文段落 {i}" for i in range(205))
        en = enrich.Enrichment(title="长帖", overview="概览", images=[
            enrich.ImageRead(index=0, informative=True, summary="图片总结", text=raw)])
        blocks = pipeline._web_blocks("https://example.org", en, [], raw, 1)
        toggles = [b["toggle"] for b in blocks if b["type"] == "toggle"]
        self.assertEqual(len(toggles), 6)
        self.assertTrue(all(len(b["children"]) <= notion.MAX_CHILDREN for b in toggles))
        for group in (toggles[:3], toggles[3:]):
            paras = [b for t in group for b in t["children"]]
            text = "\n".join(b["paragraph"]["rich_text"][0]["text"]["content"] for b in paras)
            self.assertEqual(text, raw)

    def test_missing_or_misnumbered_image_summaries_fail_even_without_schema(self):
        answer = {"title": "帖子", "overview": "概览", "key_points": ["要点"],
                  "material_type": "帖子", "images": []}
        with patch.object(enrich.settings, "active", return_value={"base_url": "test", "model": "test"}), \
             patch.object(enrich.config, "LLM_USE_SCHEMA", False), \
             patch.object(enrich, "_chat", side_effect=lambda *a, **kw: json.dumps(answer)):
            for images in ([], [{"index": 0, "informative": True, "text": "OCR"}],
                           [{"index": 1, "informative": True, "summary": "错号"}],
                           [{"index": 0, "informative": False}] * 2):
                with self.subTest(images=images):
                    answer["images"] = images
                    _, error = enrich.enrich("帖子", "正文", pipeline._web_hint("https://xhslink.cn/a", "小红书"),
                                             images=[(b"image", "image/jpeg")])
                    self.assertTrue(error)
            answer["images"] = [{"index": 0, "informative": True, "summary": "图表趋势", "text": ""}]
            _, error = enrich.enrich("帖子", "正文", pipeline._web_hint("https://xhslink.cn/a", "小红书"),
                                     images=[(b"image", "image/jpeg")])
            self.assertFalse(error)

    def test_failed_post_summary_preserves_images_and_stages(self):
        with patch.multiple(store, find_by_url=lambda _: None, pending_url_owner=lambda _: None):
            staged = staging_stub.install(store)
            with patch.object(pipeline, "fetch_for", return_value={"ok": True, "title": "帖子", "text": "引言", "images": ["https://cdn/image"]}), \
                 patch.object(pipeline.extract, "download_images", return_value=([(b"image", "image/jpeg")], [])), \
                 patch.object(enrich, "enrich", return_value=(enrich.Enrichment(title="帖子"), "逐图总结缺失")), \
                 patch.object(storage, "archive_text", return_value="gs://body"), \
                 patch.object(storage, "archive", return_value="gs://image") as archive, \
                 patch.object(pipeline, "_create_material") as create:
                pipeline._handle_url({"chat": {"id": 1}, "message_id": 1}, "https://xhslink.cn/a", "")
            archive.assert_called_once()
            create.assert_not_called()
            self.assertTrue(staged)

    def test_source_title_overrides_model_translation_and_summary_is_required(self):
        answer = {"title": "被模型翻译的标题", "overview": "作者讨论重复计算与参数共享。",
                  "key_points": ["深度共享减少独立参数，但不必然减少推理计算。"],
                  "material_type": "网页", "topics": []}
        with patch.object(enrich.settings, "active", return_value={"base_url": "test", "model": "test"}), \
             patch.object(notion, "known_topics", return_value=[]), \
             patch.object(enrich, "_chat", side_effect=lambda *a, **kw: json.dumps(answer)):
            result, error = enrich.enrich("Original Title", "source text", pipeline._web_hint("https://example.org", "网页"))
            self.assertFalse(error)
            self.assertEqual(result.title, "Original Title")
            self.assertEqual(len(result.key_points), 1)
            answer.pop("key_points")
            _, error = enrich.enrich("Original Title", "source text", pipeline._web_hint("https://example.org", "网页"))
            self.assertIn("要点", error)
            # Papers and images don't acquire the webpage-only requirement.
            _, error = enrich.enrich("Original Title", "abstract", "论文")
            self.assertFalse(error)

    def test_failed_web_summary_is_staged_after_archiving(self):
        with patch.multiple(store, find_by_url=lambda _: None, pending_url_owner=lambda _: None):
            staged = staging_stub.install(store)
            with patch.object(pipeline, "fetch_for", return_value={"ok": True, "title": "Original", "text": "Original body"}), \
                 patch.object(enrich, "enrich", return_value=(enrich.Enrichment(title="Original"), "网页中文概览或要点总结缺失")), \
                 patch.object(storage, "archive_text", return_value="gs://archive/body") as archive, \
                 patch.object(pipeline, "_create_material") as create:
                pipeline._handle_url({"chat": {"id": 1}, "message_id": 1}, "https://example.org/a", "")
            create.assert_not_called()
            self.assertTrue(staged)
            self.assertEqual(archive.call_args.args[0], "Original body")

    def test_reading_layout_and_plain_source_toggle(self):
        en = enrich.Enrichment(title="Original", overview="概览", key_points=["中文要点一", "中文要点二"])
        blocks = pipeline._web_blocks("https://example.org/article", en, ["原文归档：gs://body"])
        self.assertEqual(blocks[0]["type"], "heading_2")
        self.assertNotIn("bookmark", [b["type"] for b in blocks])
        self.assertEqual(blocks[-1]["type"], "toggle")
        self.assertNotIn("callout", [b["type"] for b in blocks])
        self.assertNotIn("原始正文", str(blocks))
        self.assertIn("中文要点二", str(blocks))
        self.assertEqual(notion.card_metadata(blocks, "网页")["卡片摘要"], "概览")
        paper = pipeline._body_blocks("概览", "Original abstract", "Abstract", ["来源"])
        self.assertIn("Original abstract", str(paper))
        self.assertEqual(paper[-1]["type"], "toggle")

    def test_material_title_only_arrow_is_linked_and_retry_preserves_it(self):
        url = "https://example.org/source"
        value = notion.material_title("Original title", url)
        self.assertEqual(value["title"][0]["text"], {"content": "Original title"})
        self.assertEqual(value["title"][-1]["text"], {"content": " ↗\ufe0e", "link": {"url": url}})
        page = {"properties": {"标题": {"type": "title", **value}, "原始链接": {"type": "url", "url": url}}}
        self.assertEqual(notion.prop_text(page, "标题"), "Original title")
        with patch.object(notion, "_call", return_value={}) as call:
            notion.refresh_machine_fields("page", title="Revised title", material_type="网页", topics=[], processing="完成", existing_page=page)
        title = call.call_args.args[2]["properties"]["标题"]
        self.assertEqual(title, notion.material_title("Revised title", url))
        self.assertEqual(notion.material_title("Photo", None), notion._title("Photo"))
        # A literal arrow in an original title, or a link to another source,
        # must not be silently removed from user content.
        page["properties"]["标题"] = {"type": "title", **notion._title("Original ↗")}
        self.assertEqual(notion.prop_text(page, "标题"), "Original ↗")
        page["properties"]["标题"] = {"type": "title", **notion.material_title("Original", "https://another.example")}
        self.assertEqual(notion.prop_text(page, "标题"), "Original ↗\ufe0e")

    def test_long_original_title_is_not_silently_shortened(self):
        title = "Original source title " * 120
        self.assertEqual("".join(p["text"]["content"] for p in notion._title(title)["title"]), title)


if __name__ == "__main__":
    unittest.main()
