"""Real login-gate captures must be staged; short readable articles still land."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx
import trafilatura

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import enrich, extract, notion, pipeline, storage, store
import staging_stub

FIXTURES = Path(__file__).parent / "fixtures" / "web_capture"
SOURCES = {row["file"]: row for row in json.loads((FIXTURES / "sources.json").read_text())}


class CaptureTests(unittest.TestCase):
    def fixture_response(self, filename):
        row = SOURCES[filename]
        return httpx.Response(200, text=(FIXTURES / filename).read_text(),
                              request=httpx.Request("GET", row["url"]))

    def test_real_gate_bodies_are_staged_without_llm_or_material_creation(self):
        for filename in ("eaa-member-gate.html", "alpha-login-gate.html"):
            with self.subTest(filename=filename):
                response = self.fixture_response(filename)
                raw = trafilatura.extract(response.text, include_comments=False,
                                           include_tables=False, favor_precision=True)
                self.assertTrue(raw, "The real extraction must reproduce the old false success")
                with patch.object(extract.httpx, "get", return_value=response):
                    result = extract.fetch_article(str(response.url))
                    self.assertFalse(result["ok"])
                    self.assertIn("登录", result["error"])
                    self.assertEqual(result["text"], "")

                    # Also exercise the caller: a rejection must land in staged,
                    # not merely return a different flag from the extractor.
                    with patch.multiple(store, find_by_url=lambda _: None,
                                        pending_url_owner=lambda _: None):
                        staged = staging_stub.install(store)
                        with patch.object(pipeline, "_create_material") as material, \
                             patch.object(enrich, "enrich") as llm, \
                             patch.object(storage, "archive_text") as archive:
                            text, _ = pipeline.dispatch({"chat": {"id": 1},
                                "message_id": 1, "text": str(response.url)}, False)
                        self.assertIn("暂存区", text)
                        self.assertTrue(staged)
                        material.assert_not_called()
                        llm.assert_not_called()
                        archive.assert_not_called()

    def test_real_short_article_lands_with_summary_and_archived_original(self):
        response = self.fixture_response("sivers-short-article.html")
        with patch.object(extract.httpx, "get", return_value=response):
            result = extract.fetch_article(str(response.url))
            self.assertTrue(result["ok"])
            self.assertLess(len(result["text"].split()), 300)
            self.assertIn("a very slow thinker", result["title"])
            with patch.multiple(store, find_by_url=lambda _: None,
                                pending_url_owner=lambda _: None,
                                index_url=lambda *a: None):
                staged = staging_stub.install(store)
                with patch.object(pipeline, "_create_material", return_value={"id": "m", "url": "n"}) as material, \
                     patch.object(enrich, "enrich", return_value=(enrich.Enrichment(
                         title="我思考得很慢", overview="短文概览", key_points=["作者需要时间形成自己的看法。"]), "")), \
                     patch.object(storage, "archive_text", return_value="gs://body") as archive:
                    pipeline.dispatch({"chat": {"id": 1}, "message_id": 2,
                                       "text": str(response.url)}, False)
                self.assertFalse(staged)
                material.assert_called_once()
                self.assertEqual(material.call_args.kwargs["title"], result["title"])
                blocks = material.call_args.kwargs["blocks"]
                self.assertNotIn(result["text"].splitlines()[0], str([b for b in blocks if b["type"] != "toggle"]))
                self.assertIn(result["text"].splitlines()[0], str(blocks))
                self.assertIn("作者需要时间形成自己的看法。", str(material.call_args.kwargs["blocks"]))
                self.assertEqual(archive.call_args.args[0], result["text"])

    def test_short_article_mentioning_login_and_cookies_is_not_a_gate(self):
        # This is a counterexample, not a fabricated failure sample. In
        # particular, even quoting the exact observed gate must not block an
        # article that supplies actual content alongside it.
        gate = (FIXTURES / "eaa-member-gate.html").read_text()
        doc = gate.replace("<p> </p>", "<p>Cookies store browser session state. Clearing them logs you out. "
                           "Login messages are useful diagnostics, not the actual article you requested.</p>")
        response = httpx.Response(200, text=doc, request=httpx.Request("GET", "https://example.org/short"))
        with patch.object(extract.httpx, "get", return_value=response):
            self.assertTrue(extract.fetch_article(str(response.url))["ok"])
        doc = "<html><head><title>短文</title></head><body><article><p>登录状态可以用 cookie 保存；退出时记得清理它。</p></article></body></html>"
        response = httpx.Response(200, text=doc, request=httpx.Request("GET", "https://example.org/short"))
        with patch.object(extract.httpx, "get", return_value=response):
            result = extract.fetch_article(str(response.url))
            self.assertTrue(result["ok"])
            self.assertLess(len(result["text"]), 200)


if __name__ == "__main__":
    unittest.main()
