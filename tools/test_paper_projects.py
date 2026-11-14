"""Paper project recognition, title checks, bilingual abstracts and retry safety."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx
from lxml import html as lhtml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import enrich, extract, notion, paper_project, pipeline, storage, store
import staging_stub

URL = "https://voyager.minedojo.org/"
HTML = (Path(__file__).parent / "fixtures/paper_projects/voyager.html").read_text()
TITLE = "Voyager: An Open-Ended Embodied Agent with Large Language Models"
ABSTRACT = "We introduce Voyager, the first LLM-powered embodied lifelong learning agent in Minecraft."


def metadata(**kw):
    return {"ok": True, "title": TITLE, "text": ABSTRACT,
            "authors": ["Guanzhi Wang"], "published": "2023-05-25", "doi": "",
            "journal_ref": "", "error": "", "pdf_urls": [], **kw}


class ProjectTests(unittest.TestCase):
    def article(self, html=HTML):
        with patch.object(extract.httpx, "get", return_value=httpx.Response(
                200, text=html, request=httpx.Request("GET", URL))):
            return extract.fetch_article(URL)

    def test_real_voyager_page_detected_without_using_domain_or_model(self):
        art = self.article()
        self.assertTrue(art["ok"])
        project = art["paper_project"]
        self.assertEqual(project["arxiv_id"], "2305.16291")
        self.assertEqual(project["title"], TITLE)
        self.assertTrue(project["self_citation"])
        self.assertIn("lifelong learning agent", project["abstract"])
        self.assertNotIn("@article", project["abstract"])
        self.assertEqual(paper_project.detect(HTML, "https://example.org/research", TITLE)["arxiv_id"], "2305.16291")

    def test_cited_paper_does_not_make_a_blog_a_paper(self):
        html = '<h1>My blog</h1><article><p>An article discussing a paper.</p><a href="https://arxiv.org/abs/2305.16291">Paper</a></article>'
        candidate = paper_project.detect(html, URL, "My blog")
        with patch.object(extract, 'fetch_arxiv', return_value=metadata()):
            self.assertTrue(paper_project.resolve(candidate)['not_project'])
        project = self.article()["paper_project"]
        project["title"] = "My paper review"
        with patch.object(extract, "fetch_arxiv", return_value=metadata()), \
             patch.object(extract, "semantic_scholar") as scholar:
            self.assertFalse(paper_project.resolve(project)["ok"])
            scholar.assert_not_called()
        project["self_citation"] = False
        with patch.object(extract, "fetch_arxiv", return_value=metadata()):
            self.assertTrue(paper_project.resolve(project)["not_project"])

    def test_ambiguous_multiple_papers_not_promoted(self):
        html = HTML + '<a href="https://arxiv.org/abs/1706.03762">Paper</a>'
        self.assertFalse(paper_project.detect(html, URL, TITLE))

    def test_matching_bibtex_identifies_project_even_without_local_abstract(self):
        html = HTML.replace('<h2 class="title is-3">Abstract', '<h2 class="title is-3">Overview')
        project = paper_project.detect(html, URL, TITLE)
        self.assertTrue(project["self_citation"])
        self.assertEqual(project["abstract"], "")

    def test_citation_metadata_supports_doi_project(self):
        html = '<meta name="citation_title" content="Study"><meta name="citation_doi" content="10.1234/study"><meta name="citation_author" content="A Researcher"><article><p>A project description.</p></article>'
        project = paper_project.detect(html, URL, "Study")
        self.assertEqual(project["doi"], "10.1234/study")
        self.assertTrue(project["self_citation"])
        self.assertEqual(project["authors"], ["A Researcher"])

    def test_official_abstract_preferred_to_short_project_abstract(self):
        project = self.article()["paper_project"]
        with patch.object(extract, "fetch_arxiv", return_value=metadata()), \
             patch.object(extract, "semantic_scholar", return_value={}):
            meta = paper_project.resolve(project)
        self.assertEqual(meta["text"], ABSTRACT)
        self.assertEqual(meta["abstract_source"], "https://arxiv.org/abs/2305.16291")

    def test_api_outage_needs_self_citation_for_local_abstract_fallback(self):
        project = self.article()["paper_project"]
        with patch.object(extract, "fetch_arxiv", return_value=metadata(ok=False, error="timeout")), \
             patch.object(paper_project, 'fetch_arxiv_landing', return_value={'ok': False, 'error': 'offline'}), \
             patch.object(extract, "semantic_scholar", return_value={}):
            meta = paper_project.resolve(project)
            self.assertTrue(meta["ok"])
            self.assertEqual(meta["text"], project["abstract"])
            self.assertEqual(meta["abstract_source"], "项目页 Abstract")
            project["self_citation"] = False
            self.assertFalse(paper_project.resolve(project)["ok"])

    def test_no_abstract_is_staged_before_model_or_creation(self):
        art = self.article()
        art["paper_project"]["abstract"] = ""
        with patch.object(extract, "fetch_arxiv", return_value=metadata(text="")), \
             patch.object(extract, "semantic_scholar", return_value={}):
            meta = paper_project.resolve(art["paper_project"])
        with patch.multiple(store, find_by_url=lambda _: None, pending_url_owner=lambda _: None):
            staged = staging_stub.install(store)
            with patch.object(pipeline, "fetch_for", return_value=art), \
                 patch.object(paper_project, "resolve", return_value=meta), \
                 patch.object(enrich, "enrich") as model, \
                 patch.object(pipeline, "_create_material") as create:
                pipeline._handle_url({"chat": {"id": 1}, "message_id": 1}, URL, "")
        self.assertTrue(staged)
        model.assert_not_called()
        create.assert_not_called()

    def test_project_summary_and_full_abstract_are_kept_in_one_page(self):
        art = self.article()
        meta = {**metadata(), "ident": "arXiv:2305.16291", "abstract_source": "arXiv"}
        en = enrich.Enrichment(title=TITLE, overview="项目概览", key_points=["项目网页报告 2–6 倍。"],
                               abstract_zh="冻结的 RGB VAE 几乎无损编码 3D 点图。结果提高 2–7 倍。")
        with patch.multiple(store, find_by_url=lambda _: None, pending_url_owner=lambda _: None):
            staging_stub.install(store)
            with patch.object(pipeline, "fetch_for", return_value=art), \
                 patch.object(paper_project, "resolve", return_value=meta), \
                 patch.object(storage, "archive_text", return_value="gs://body") as archive, \
                 patch.object(enrich, "enrich", return_value=(en, "")) as model, \
                 patch.object(pipeline, "_create_material", return_value={"id": "p", "url": "n"}) as create:
                pipeline._handle_url({"chat": {"id": 1}, "message_id": 1}, URL, "")
        self.assertEqual(model.call_args.kwargs["paper_abstract"], ABSTRACT)
        self.assertEqual(model.call_args.args[1], art["text"])
        self.assertEqual(archive.call_count, 2)
        kw = create.call_args.kwargs
        self.assertEqual(kw["material_type"], "论文")
        self.assertEqual(kw["url"], URL)
        self.assertIn(ABSTRACT, str(kw["blocks"]))
        self.assertIn(en.abstract_zh, str(kw["blocks"]))
        self.assertIn("2–6", str(kw["blocks"]))
        self.assertEqual(kw["extra_props"]["作者"]["rich_text"][0]["text"]["content"], "Guanzhi Wang")

    def test_translation_is_required_only_when_an_abstract_is_provided(self):
        answer = {"title": TITLE, "overview": "概览", "key_points": ["要点"], "material_type": "网页", "topics": []}
        with patch.object(enrich.settings, "active", return_value={"base_url": "test", "model": "test"}), \
             patch.object(notion, "known_topics", return_value=[]), \
             patch.object(enrich, "_chat", side_effect=lambda *a, **kw: json.dumps(answer)) as model:
            _, error = enrich.enrich(TITLE, "project body", pipeline._web_hint(URL, "网页"), paper_abstract=ABSTRACT)
            self.assertIn("翻译", error)
            schema = model.call_args.kwargs["json_schema"]["schema"]
            self.assertIn("abstract_zh", schema["required"])
            self.assertIn(ABSTRACT, model.call_args.args[0][1]["content"])
            answer["abstract_zh"] = "完整中文译文"
            en, error = enrich.enrich(TITLE, "project body", pipeline._web_hint(URL, "网页"), paper_abstract=ABSTRACT)
            self.assertFalse(error)
            self.assertEqual(en.material_type, "论文")
            answer.pop("abstract_zh")
            _, error = enrich.enrich("ordinary article", "body", pipeline._web_hint(URL, "网页"))
            self.assertFalse(error)

    def test_failed_translation_stages_url_without_attached_thought(self):
        art = self.article()
        meta = {**metadata(), "ident": "arXiv:2305.16291", "abstract_source": "arXiv"}
        with patch.multiple(store, find_by_url=lambda _: None, pending_url_owner=lambda _: None):
            staged = staging_stub.install(store)
            with patch.object(storage, "archive_text", return_value="gs://body"), \
                 patch.object(enrich, "enrich", return_value=(enrich.Enrichment(title=TITLE), "论文摘要中文翻译缺失")), \
                 patch.object(pipeline, "_create_material") as create, \
                 patch.object(pipeline, "_save_link_thought", return_value={"id": "thought", "url": "note"}) as save:
                pipeline._handle_project_paper({"chat": {"id": 1}, "message_id": 1}, URL, art, meta, "值得看看几何编码", lambda _: None)
        create.assert_not_called()
        self.assertEqual(store.get_staged(URL)["thought_ids"], [])
        save.assert_not_called()

    def test_real_cvf_project_uses_official_cvf_abstract(self):
        html = (Path(__file__).parent / 'fixtures/paper_projects/dust3r.html').read_text()
        art = self.article(html)
        self.assertEqual(art['title'], 'DUSt3R: Geometric 3D Vision Made Easy')
        cvf = (Path(__file__).parent / 'fixtures/paper_projects/dust3r-cvf.html').read_text()
        landing = art['paper_project']['cvf_url']
        official = paper_project.parse_cvf(cvf, landing)
        self.assertTrue(official['ok'])
        self.assertIn('Multi-view stereo reconstruction', official['text'])
        self.assertIn('2024', official['journal_ref'])
        self.assertEqual(official['published'], '')  # Year-only must not invent a date.
        with patch.object(paper_project, 'fetch_cvf', return_value=official), patch.object(extract, 'semantic_scholar') as scholar:
            resolved = paper_project.resolve(art['paper_project'])
        self.assertTrue(resolved['ok'])
        self.assertEqual(resolved['abstract_source'], landing)
        scholar.assert_not_called()

    def test_real_project_without_abstract_or_identifier_in_bibtex(self):
        base = Path(__file__).parent / 'fixtures/paper_projects'
        for name, pid in [('pix2pix', '1611.07004'), ('taming-transformers', '2012.09841')]:
            with self.subTest(name=name):
                art = self.article((base / (name+'.html')).read_text())
                project = art['paper_project']
                self.assertEqual(project['arxiv_id'], pid)
                self.assertFalse(project['self_citation'])
                official = json.loads((base / (pid+'.json')).read_text())
                with patch.object(extract, 'fetch_arxiv', return_value=official), patch.object(extract, 'semantic_scholar', return_value={}):
                    resolved = paper_project.resolve(project)
                self.assertTrue(resolved['ok'])
                self.assertEqual(resolved['text'], official['text'])
                self.assertTrue(paper_project.same_title(art['title'], official['title']))
                with patch.object(extract, 'fetch_arxiv', return_value={'ok': False, 'error': 'timeout'}), \
                     patch.object(paper_project, 'fetch_arxiv_landing', return_value={'ok': False, 'error': 'offline'}):
                    self.assertFalse(paper_project.resolve(project)['ok'])

    def test_title_metadata_and_inline_heading_preserve_words(self):
        self.assertEqual(paper_project.page_title('<title>Short [ICRA]</title><h1>Full<br>Paper title</h1>'), 'Full Paper title')
        self.assertEqual(paper_project.page_title('<h1>P<span>ROGRESSOR</span>: Title</h1>'), 'PROGRESSOR: Title')
        self.assertEqual(paper_project.page_title('<meta name="title" content="Full title"><meta property="og:title" content="Short"><h2>Abstract</h2>'), 'Full title')
        self.assertEqual(paper_project.page_title('<h1>Abstract</h1><title>Real title</title>'), 'Real title')

    def test_cvf_requires_canonical_paper_path_and_matching_metadata(self):
        self.assertFalse(paper_project.cvf_landing_url('https://openaccess.thecvf.com.attacker.org/content/ICCV2025/papers/P_paper.pdf'))
        self.assertFalse(paper_project.cvf_landing_url('https://openaccess.thecvf.com/menu.pdf'))
        base = Path(__file__).parent / 'fixtures/paper_projects'
        html = (base / 'dust3r-cvf.html').read_text()
        self.assertFalse(paper_project.parse_cvf(html, 'https://openaccess.thecvf.com/content/CVPR2024/html/Other_paper.html')['ok'])

    def test_branded_llava_header_matches_its_paper_subtitle(self):
        html = (Path(__file__).parent / 'fixtures/paper_projects/llava.html').read_text()
        project = paper_project.detect(html, 'https://llava-vl.github.io/', '')
        self.assertEqual(project['arxiv_id'], '2304.08485')
        self.assertEqual(project['title'], 'LLaVA: Large Language and Vision Assistant')
        self.assertIn('Visual Instruction Tuning', project['title_variants'])
        self.assertNotIn('Improved Baselines with Visual Instruction Tuning', project['title_variants'])
        self.assertFalse(project['self_citation'])  # No unverified local fallback.
        official = metadata(title='Visual Instruction Tuning')
        with patch.object(extract, 'fetch_arxiv', return_value=official), \
             patch.object(extract, 'semantic_scholar', return_value={}):
            result = paper_project.resolve(project)
        self.assertTrue(result['ok'])
        self.assertEqual(result['text'], ABSTRACT)

    def test_llava_subtitle_still_requires_official_title_and_api(self):
        html = (Path(__file__).parent / 'fixtures/paper_projects/llava.html').read_text()
        project = paper_project.detect(html, 'https://llava-vl.github.io/', '')
        for official in [metadata(title='Unrelated paper'), metadata(ok=False, error='offline')]:
            with self.subTest(official=official['ok']), \
                 patch.object(extract, 'fetch_arxiv', return_value=official), \
                 patch.object(paper_project, 'fetch_arxiv_landing', return_value={'ok': False, 'error': 'offline'}), \
                 patch.object(extract, 'semantic_scholar') as scholar:
                self.assertFalse(paper_project.resolve(project)['ok'])
                scholar.assert_not_called()

    def test_body_heading_or_bibtex_alone_cannot_override_main_title(self):
        for markup in [
            '<h1>My review</h1><p>Review text</p><h3 class="publication-title">Study</h3>',
            '<h1>My review</h1><h3>Study</h3>',
            '<h1>My review</h1><h3 class="publication-title">Different title</h3>',
        ]:
            html = markup + '<a href="https://arxiv.org/abs/2304.08485">Paper</a><pre>@misc{study, title={Study}, year={2023}}</pre>'
            with self.subTest(markup=markup):
                project = paper_project.detect(html, URL, '')
                self.assertEqual(project['title_variants'], ['My review'])
                with patch.object(extract, 'fetch_arxiv', return_value=metadata(title='Study')):
                    self.assertTrue(paper_project.resolve(project)['not_project'])

    def test_llava_still_promotes_ambiguous_primary_links_to_nothing(self):
        html = (Path(__file__).parent / 'fixtures/paper_projects/llava.html').read_text()
        html += '<a href="https://arxiv.org/abs/2310.03744">Paper</a>'
        self.assertFalse(paper_project.detect(html, URL, ''))

    def test_llava_enters_bilingual_paper_path_preserving_project_title(self):
        html = (Path(__file__).parent / 'fixtures/paper_projects/llava.html').read_text()
        art = self.article(html)
        official = {**metadata(title='Visual Instruction Tuning'), 'ident': 'arXiv:2304.08485', 'abstract_source': 'arXiv'}
        en = enrich.Enrichment(title=art['title'], overview='概览', abstract_zh='完整译文')
        with patch.multiple(store, find_by_url=lambda _: None, pending_url_owner=lambda _: None):
            staging_stub.install(store)
            with patch.object(pipeline, 'fetch_for', return_value=art), \
                 patch.object(paper_project, 'resolve', return_value=official), \
                 patch.object(storage, 'archive_text', return_value='gs://body'), \
                 patch.object(enrich, 'enrich', return_value=(en, '')) as model, \
                 patch.object(pipeline, '_create_material', return_value={'id': 'p', 'url': 'n'}) as create:
                pipeline._handle_url({'chat': {'id': 1}, 'message_id': 1}, URL, '')
        self.assertEqual(create.call_args.kwargs['title'], 'LLaVA: Large Language and Vision Assistant')
        self.assertEqual(create.call_args.kwargs['material_type'], '论文')
        self.assertEqual(model.call_args.kwargs['paper_abstract'], ABSTRACT)
        self.assertIn('完整译文', str(create.call_args.kwargs['blocks']))

    def test_official_arxiv_citation_fallback_is_complete_and_identity_checked(self):
        html = (Path(__file__).parent / 'fixtures/paper_projects/llava-arxiv.html').read_text()
        meta = paper_project.parse_arxiv_landing(html, '2304.08485')
        self.assertTrue(meta['ok'])
        self.assertEqual(meta['title'], 'Visual Instruction Tuning')
        self.assertEqual(meta['authors'], ['Haotian Liu', 'Chunyuan Li', 'Qingyang Wu', 'Yong Jae Lee'])
        self.assertEqual(meta['published'], '2023-04-17')
        self.assertEqual(meta['text'], lhtml.fromstring(html).xpath('//meta[@name="citation_abstract"]/@content')[0].strip())
        self.assertNotIn('arXivLabs', meta['text'])
        self.assertFalse(paper_project.parse_arxiv_landing(html, '2310.03744')['ok'])
        self.assertFalse(paper_project.parse_arxiv_landing(html.replace('name="citation_abstract"', 'name="not_abstract"'), '2304.08485')['ok'])

    def test_arxiv_api_rate_limit_uses_official_fields_not_project_summary(self):
        html = (Path(__file__).parent / 'fixtures/paper_projects/llava.html').read_text()
        project = paper_project.detect(html, 'https://llava-vl.github.io/', '')
        official = (Path(__file__).parent / 'fixtures/paper_projects/llava-arxiv.html').read_text()
        expected = paper_project.parse_arxiv_landing(official, project['arxiv_id'])
        with patch.object(extract, 'fetch_arxiv', return_value=metadata(ok=False, error='429')), \
             patch.object(paper_project, 'fetch_arxiv_landing', return_value=expected), \
             patch.object(extract, 'semantic_scholar', return_value={}):
            resolved = paper_project.resolve(project)
        self.assertTrue(resolved['ok'])
        self.assertEqual(resolved['text'], expected['text'])
        self.assertNotEqual(resolved['text'], project['abstract'])

    def test_official_fallback_rejects_cross_host_or_different_paper_redirects(self):
        html = (Path(__file__).parent / 'fixtures/paper_projects/llava-arxiv.html').read_text()
        for url in ['https://example.com/abs/2304.08485', 'https://arxiv.org/abs/2310.03744']:
            with self.subTest(url=url), patch.object(httpx, 'get',
                    return_value=httpx.Response(200, text=html, request=httpx.Request('GET', url))):
                self.assertFalse(paper_project.fetch_arxiv_landing('2304.08485')['ok'])


if __name__ == "__main__":
    unittest.main()
