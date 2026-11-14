"""Display image rules, genuine codecs/PDFs, and decoration failure isolation."""

import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
import pillow_heif
from pypdf import PdfWriter
from app import config, enrich, notion, pdf_art, pipeline, thumbnails, work_budget
from app.art_categories import ART_CATEGORIES, fallback_category

FIXTURES = Path(__file__).parent / "fixtures" / "cover_art"


def png(size=(400, 300), color="blue"):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class Images(unittest.TestCase):
    def setUp(self):
        thumbnails._memory.clear()
        thumbnails._downloads.clear()

    def test_actual_decode_rejects_fake_headers_and_html(self):
        for data in (b"\xff\xd8\xffnot-a-jpeg", b"<html>404</html>"):
            with self.assertRaises(Exception):
                thumbnails.normalize(data)

    def test_heic_ico_svg_orientation_and_dimensions(self):
        im = Image.new("RGB", (400, 200), "red")
        out = io.BytesIO()
        pillow_heif.register_heif_opener()
        im.save(out, format="HEIF")
        data, mime, sz = thumbnails.normalize(out.getvalue())
        self.assertEqual(sz, (400, 200))
        self.assertEqual(mime, "image/jpeg")
        out = io.BytesIO()
        im.resize((64, 64)).save(out, "ICO")
        self.assertEqual(thumbnails.normalize(out.getvalue())[2], (64, 64))
        self.assertEqual(
            thumbnails.normalize((thumbnails.ASSETS / "zhihu.svg").read_bytes())[2],
            (512, 512),
        )
        im = Image.new("RGB", (80, 40))
        exif = im.getexif()
        exif[274] = 6
        out = io.BytesIO()
        im.save(out, "JPEG", exif=exif)
        self.assertEqual(thumbnails.normalize(out.getvalue())[2], (40, 80))

    def test_svg_cannot_fetch_external_resources(self):
        for payload in (
            b'<svg xmlns="http://www.w3.org/2000/svg"><image href="http://localhost/secret"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><path fill="url(http://localhost/a)"/></svg>',
        ):
            with self.assertRaises(ValueError):
                thumbnails.normalize(payload)

    def test_transparent_content_keeps_alpha_and_ratio(self):
        out = io.BytesIO()
        im = Image.new("RGBA", (400, 200), (255, 0, 0, 0))
        im.putpixel((200, 100), (255, 0, 0, 255))
        im.save(out, "PNG")
        data, mime, size = thumbnails.normalize(out.getvalue())
        result = Image.open(io.BytesIO(data))
        self.assertEqual(mime, "image/png")
        self.assertEqual(size, (400, 200))
        self.assertEqual(result.size, (400, 200))
        self.assertEqual(result.getpixel((0, 0))[3], 0)
        self.assertEqual(result.getpixel((200, 100))[3], 255)

    def test_content_ratio_and_tiny_logo_canvas(self):
        data, _, size = thumbnails.normalize(png((800, 300)))
        self.assertEqual(Image.open(io.BytesIO(data)).size, (800, 300))
        logo, _, _ = thumbnails.normalize(png((16, 16)), "logo_cover")
        im = Image.open(io.BytesIO(logo))
        self.assertEqual(im.size, (600, 400))
        self.assertNotEqual(im.getpixel((300, 200)), im.getpixel((0, 0)))

    def test_body_scope_lazy_srcset_relative_and_icon_priority(self):
        body = "A real article with a substantial paragraph to locate its body."
        html = f"""<link rel="shortcut icon" sizes="16x16" href="/tiny.ico"><link rel="icon" sizes="128x128" href="/large.svg">
        <nav><img src="/nav.png"></nav><article><p>{body}</p><img data-src="/lazy.jpg" src="data:image/gif,x" srcset="/small.jpg 300w, /big.jpg 900w">
        <div class="comments"><img src="/avatar.jpg"></div><img width="1" height="1" src="/track.gif"></article>"""
        art = thumbnails.html_candidates(html, "https://blog.example/post", body)
        self.assertEqual(
            art["body"],
            [
                "https://blog.example/big.jpg",
                "https://blog.example/small.jpg",
                "https://blog.example/lazy.jpg",
            ],
        )
        self.assertEqual(art["logos"][0], "https://blog.example/large.svg")
        self.assertNotIn("https://blog.example/nav.png", art["body"])

    def test_generic_share_logo_demoted_and_no_guessed_body(self):
        art = thumbnails.html_candidates(
            '<meta property="og:image" content="/logo.png"><img src="/avatar.jpg">',
            "https://e.test/post",
            "body",
        )
        self.assertEqual(art["shares"], [])
        self.assertEqual(art["body"], [])
        self.assertIn("https://e.test/logo.png", art["logos"])

    def test_shared_site_og_is_demoted_using_real_metadata(self):
        site = "https://sive.rs/slow"
        homepage = b'<meta property="og:image" content="https://m.sive.rs/og.jpg">'
        with (
            patch.object(thumbnails, "download", return_value=homepage),
            patch("app.store.db", side_effect=RuntimeError("offline")),
        ):
            self.assertEqual(
                thumbnails.site_generic_images(site), ["https://m.sive.rs/og.jpg"]
            )
        calls = []

        def upload(src, mode="cover", **kw):
            calls.append(src)
            return {"type": "file_upload", "file_upload": {"id": "good"}}

        with (
            patch.object(config, "NOTION_TOKEN", "test"),
            patch.object(
                thumbnails,
                "site_generic_images",
                return_value=["https://m.sive.rs/og.jpg"],
            ),
            patch.object(thumbnails, "upload_image", side_effect=upload),
        ):
            thumbnails.material_art(
                "网页",
                "网页",
                site,
                {
                    "shares": ["https://m.sive.rs/og.jpg"],
                    "body": ["article-image"],
                    "logos": ["site-logo"],
                },
            )
            self.assertEqual(calls[0], "article-image")
            self.assertNotIn("https://m.sive.rs/og.jpg", calls)

    def test_first_candidate_fails_then_next_and_platform_placeholder(self):
        calls = []

        def upload(src, mode="cover", **kwargs):
            calls.append((src, mode, kwargs))
            if src == "broken":
                raise ValueError("bad")
            return {"type": "file_upload", "file_upload": {"id": "good"}}

        with (
            patch.object(config, "NOTION_TOKEN", "test"),
            patch.object(thumbnails, "upload_image", side_effect=upload),
        ):
            art = thumbnails.material_art(
                "帖子",
                "知乎",
                "https://zhihu.com/q",
                {"shares": ["broken"], "body": ["body"]},
            )
            self.assertEqual(art["thumbnail"]["file_upload"]["id"], "good")
            self.assertEqual(
                [c[0] for c in calls if isinstance(c[0], str)], ["broken", "body"]
            )
            calls.clear()
            thumbnails.material_art("帖子", "知乎", "https://zhihu.com/q", {})
            self.assertIn("placeholder-post", calls[-1][2]["cache_key"])

    def test_only_favicon_and_everything_missing(self):
        calls = []

        def upload(src, mode="cover", **kwargs):
            calls.append((src, mode, kwargs))
            return {"type": "file_upload", "file_upload": {"id": "good"}}

        with (
            patch.object(config, "NOTION_TOKEN", "test"),
            patch.object(thumbnails, "upload_image", side_effect=upload),
        ):
            thumbnails.material_art(
                "网页",
                "网页",
                "https://sub.e.test/post",
                {"logos": ["favicon"], "site": "https://sub.e.test/landed"},
            )
            self.assertEqual([x[1] for x in calls], ["icon", "cover"])
            self.assertIn("sub.e.test", calls[0][2]["cache_key"])
            self.assertIn("placeholder-article", calls[1][2]["cache_key"])
            calls.clear()
            thumbnails.material_art("网页", "网页", "https://e.test", {})
            self.assertIn("placeholder-article", calls[-1][2]["cache_key"])

    def test_category_uses_content_with_reliable_source_hints(self):
        self.assertEqual(
            fallback_category("帖子", "tutorial", platform="知乎"), "tutorial"
        )
        self.assertEqual(
            fallback_category("网页", "document", platform="微信"), "document"
        )
        self.assertEqual(fallback_category("其他", source_kind="document"), "document")
        self.assertEqual(
            fallback_category("论文", "document", source_kind="paper"), "paper"
        )
        self.assertEqual(fallback_category("图片", "paper"), "image")
        self.assertEqual(fallback_category("其他", "nonsense"), "other")

    def test_arxiv_and_doi_share_paper_fallback_keep_source_icon(self):
        calls = []

        def upload(src, mode="cover", **kwargs):
            calls.append((mode, kwargs.get("cache_key", "")))
            return {"type": "file_upload", "file_upload": {"id": "x"}}

        with (
            patch.object(config, "NOTION_TOKEN", "test"),
            patch.object(thumbnails, "upload_image", side_effect=upload),
        ):
            for url in (
                "https://arxiv.org/abs/1706.03762",
                "https://doi.org/10.1/example",
            ):
                thumbnails.material_art("论文", "论文", url, source_kind="paper")
        self.assertIn("asset:arxiv:icon:v3", calls[0][1])
        self.assertIn("asset:paper:icon:v3", calls[2][1])
        self.assertIn("placeholder-paper:cover:v3", calls[1][1])
        self.assertEqual(calls[1], calls[3])

    def test_received_nonpaper_pdf_fallback_uses_document_category(self):
        calls = []

        def upload(src, mode="cover", **kwargs):
            calls.append(kwargs.get("cache_key", ""))
            return {"type": "file_upload", "file_upload": {"id": "x"}}

        with (
            patch.object(config, "NOTION_TOKEN", "test"),
            patch.object(thumbnails, "upload_image", side_effect=upload),
            patch.object(pdf_art, "pdf_thumbnail", return_value=None),
        ):
            thumbnails.material_art(
                "其他", "论文", pdf_bytes=b"broken", source_kind="document"
            )
        self.assertEqual(
            calls, ["asset:pdf:icon:v3", "asset:placeholder-document:cover:v3"]
        )

    def test_all_category_assets_decode_as_opaque_content_cards(self):
        for category in ART_CATEGORIES:
            data, mime, size = thumbnails.normalize(
                (thumbnails.ASSETS / f"placeholder-{category}.svg").read_bytes()
            )
            image = Image.open(io.BytesIO(data))
            self.assertEqual(mime, "image/png")
            self.assertEqual(size, (512, 341))
            self.assertEqual(image.getpixel((0, 0))[3], 255)

    def test_optional_display_metadata_never_rejects_valid_enrichment(self):
        import json

        base = {
            "title": "教程",
            "overview": "如何操作。",
            "material_type": "网页",
            "topics": [],
        }
        for extra, expected in [
            ({}, ""),
            ({"fallback_category": "tutorial"}, "tutorial"),
            ({"fallback_category": None}, ""),
            ({"fallback_category": ["paper"]}, ""),
        ]:
            with (
                patch.object(
                    enrich.settings,
                    "active",
                    return_value={"base_url": "test", "model": "test"},
                ),
                patch.object(notion, "known_topics", return_value=[]),
                patch.object(
                    enrich, "_chat", return_value=json.dumps({**base, **extra})
                ) as chat,
            ):
                result, error = enrich.enrich("操作教程", "真实正文。")
            self.assertEqual(error, "")
            self.assertEqual(result.title, "操作教程")
            self.assertEqual(result.fallback_category, expected)
            self.assertEqual(chat.call_count, 1)

    def test_archived_cached_upload_is_not_reused(self):
        thumbnails._memory["k"] = {
            "id": "old",
            "at": __import__("time").time(),
            "checked": 0,
        }
        with (
            patch.object(config, "NOTION_TOKEN", "test"),
            patch.object(
                notion, "_call", return_value={"status": "uploaded", "archived": True}
            ),
        ):
            self.assertIsNone(thumbnails._cache_get("k"))

    def test_display_failure_after_create_never_recreates(self):
        with (
            patch.object(
                notion, "create_material", return_value={"id": "page", "url": "n"}
            ) as create,
            patch.object(notion, "append_blocks"),
            patch.object(
                thumbnails, "material_art", side_effect=RuntimeError("decoration")
            ),
        ):
            p = pipeline._create_material(
                title="body",
                material_type="网页",
                platform="网页",
                url="https://e.test",
                topics=[],
                processing="完成",
                blocks=[],
            )
            self.assertEqual(p["id"], "page")
            self.assertEqual(create.call_count, 1)

    def test_upload_rechecks_budget_after_upload_creation(self):
        clock = [0.0]
        def create(*args):
            clock[0] += 2
            return {"id": "upload", "upload_url": "https://upload.example"}
        with patch.object(work_budget.time, "monotonic", side_effect=lambda: clock[0]), work_budget.limit(8), patch.object(notion, "_call", side_effect=create), patch.object(notion.httpx, "post", return_value=Mock(status_code=200)) as send:
            self.assertEqual(notion.upload_file(b"png", "a.png", "image/png"), "upload")
            self.assertEqual(send.call_args.kwargs["timeout"], 1)
        clock[0] = 0
        def too_slow(*args):
            clock[0] += 4
            return {"id": "upload", "upload_url": "https://upload.example"}
        with patch.object(work_budget.time, "monotonic", side_effect=lambda: clock[0]), work_budget.limit(8), patch.object(notion, "_call", side_effect=too_slow), patch.object(notion.httpx, "post") as send:
            self.assertIsNone(notion.upload_file(b"png", "a.png", "image/png"))
            send.assert_not_called()
        clock[0] = 0
        with patch.object(work_budget.time, "monotonic", side_effect=lambda: clock[0]), work_budget.limit(5), patch.object(notion, "_call") as create, patch.object(notion.httpx, "post") as send:
            self.assertIsNone(notion.upload_file(b"png", "a.png", "image/png"))
            create.assert_not_called()
            send.assert_not_called()

    def test_download_uses_remaining_budget_and_stops_stream_on_expiry(self):
        reply = Mock(headers={})
        clock = [0.0]
        def chunks():
            yield b"first"
            clock[0] = 3.1  # Only the five-second write reserve remains.
            yield b"late"
        reply.iter_bytes.side_effect = chunks
        stream = Mock()
        stream.__enter__ = Mock(return_value=reply)
        stream.__exit__ = Mock(return_value=False)
        with patch.object(work_budget.time, "monotonic", side_effect=lambda: clock[0]), work_budget.limit(8), patch.object(thumbnails.httpx, "stream", return_value=stream) as get:
            with self.assertRaises(work_budget.Exhausted):
                thumbnails.download("https://image.example/test.png")
            self.assertEqual(get.call_args.kwargs["timeout"], 3)
            self.assertNotIn("https://image.example/test.png", thumbnails._downloads)

    def test_eight_seconds_skips_decoration_and_writes_note_entry_first(self):
        events = []
        with work_budget.limit(8), patch.object(config, "NOTION_TOKEN", "test"), patch.object(notion, "create_material", side_effect=lambda **kw: events.append("body") or {"id": "page", "url": "n"}), patch("app.telegram.note_deeplink", return_value="https://t.me/bot?start=note_page"), patch.object(notion, "append_blocks", side_effect=lambda *a: events.append("note entry")), patch.object(thumbnails, "upload_image") as upload, patch.object(notion, "decorate_page") as decorate:
            page = pipeline._create_material(title="body", material_type="网页", platform="网页", url="https://e.test", topics=[], processing="完成", blocks=[], art={"candidates": {"shares": ["https://image.example/a.png"]}})
        self.assertEqual(page['id'], 'page')
        self.assertEqual(events, ["body", "note entry"])
        upload.assert_not_called()
        decorate.assert_not_called()
        events.clear()
        with patch.object(notion, "create_material", side_effect=lambda **kw: events.append("body") or {"id": "page", "url": "n"}), patch("app.telegram.note_deeplink", return_value="https://t.me/bot?start=note_page"), patch.object(notion, "append_blocks", side_effect=lambda *a: events.append("note entry")), patch.object(thumbnails, "material_art", side_effect=lambda *a, **k: events.append("art") or {"icon": {"type": "emoji", "emoji": "📎"}}), patch.object(notion, "decorate_page", side_effect=lambda *a, **k: events.append("decorate")):
            pipeline._create_material(title="body", material_type="网页", platform="网页", url="https://e.test", topics=[], processing="完成", blocks=[])
        self.assertEqual(events, ["body", "note entry", "art", "decorate"])

    def test_decoration_reserves_followup_writes_and_fallback_cannot_bypass_deadline(self):
        clock = [0.0]
        def slow(*args, **kwargs):
            self.assertLessEqual(work_budget.remaining(), 15)
            clock[0] += 11
            raise TimeoutError("slow image")
        with patch.object(work_budget.time, "monotonic", side_effect=lambda: clock[0]), work_budget.limit(30), patch.object(config, "NOTION_TOKEN", "test"), patch.object(thumbnails, "upload_image", side_effect=slow) as upload:
            art = thumbnails.material_art("网页", "网页", "https://e.test", {"body": ["slow", "second"], "logos": ["logo"]})
            self.assertEqual(upload.call_count, 1)
            self.assertNotIn("thumbnail", art)
            self.assertGreaterEqual(work_budget.remaining(), 15)

    def test_base_page_never_uses_unrelated_stock_art(self):
        with patch.object(notion, "_call", return_value={"id": "known"}) as call:
            notion.create_material(
                title="论文",
                material_type="论文",
                platform="论文",
                url=None,
                topics=[],
                processing="完成",
                blocks=[],
            )
        payload = call.call_args.args[2]
        self.assertNotIn("缩略图", payload["properties"])
        self.assertEqual(payload["icon"]["type"], "emoji")

    def test_icon_patch_failure_retains_content_and_uses_default(self):
        calls = []

        def call(method, path, payload):
            calls.append(payload)
            if payload.get("icon", {}).get("type") == "file_upload":
                raise RuntimeError("expired")

        with patch.object(notion, "_call", side_effect=call):
            notion.decorate_page(
                "known", icon={"type": "file_upload", "file_upload": {"id": "expired"}}
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1]["icon"]["type"], "emoji")

    def test_missing_paper_pdf_does_not_change_admission(self):
        with patch.object(config, "NOTION_TOKEN", ""):
            self.assertEqual(
                thumbnails.paper_pdf("https://arxiv.org/abs/1706.03762", []), (None, "")
            )

    def test_photo_original_is_reused_without_url_download(self):
        with (
            patch.object(config, "NOTION_TOKEN", "test"),
            patch.object(
                thumbnails,
                "upload_image",
                return_value={"type": "file_upload", "file_upload": {"id": "x"}},
            ) as upload,
        ):
            blob = png()
            thumbnails.material_art("图片", "网页", image_bytes=blob)
            self.assertEqual(upload.call_args_list[1].args[0], blob)


class PDFs(unittest.TestCase):
    def test_real_complete_diagram_and_vector_multipanel(self):
        for name, expected in [
            ("attention", "figure"),
            ("faster-rcnn", "figure"),
            ("optuna", "first_page"),
            ("plos", "figure"),
        ]:
            image, meta = pdf_art.render((FIXTURES / (name + ".pdf")).read_bytes())
            self.assertEqual(meta["mode"], expected, name)
            self.assertGreater(len(image), 1000)
        _, meta = pdf_art.render((FIXTURES / "plos.pdf").read_bytes())
        self.assertEqual(meta["page"], 2)  # publisher's logo is on page 1

    def test_no_figure_blank_or_scan_falls_back(self):
        w = PdfWriter()
        w.add_blank_page(612, 792)
        buf = io.BytesIO()
        w.write(buf)
        image, meta = pdf_art.render(buf.getvalue())
        self.assertEqual(meta["mode"], "first_page")
        self.assertIsNotNone(pdf_art.pdf_thumbnail(buf.getvalue()))
        # A raster-only PDF made from a genuine paper page has no text layer.
        # The thumbnail must retain the entire first scanned page.
        import pypdfium2

        with pypdfium2.PdfDocument((FIXTURES / "attention.pdf").read_bytes()) as doc:
            scan = doc[0].render(scale=1).to_pil().convert("RGB")
            raster_pdf = io.BytesIO()
            scan.save(raster_pdf, "PDF")
        _, scanned = pdf_art.render(raster_pdf.getvalue())
        self.assertEqual(scanned["mode"], "first_page")

        self.assertIsNone(pdf_art.pdf_thumbnail(b"broken"))
        self.assertIsNone(pdf_art.pdf_thumbnail(b"x" * (pdf_art.MAX_PDF_BYTES + 1)))


if __name__ == "__main__":
    unittest.main()
