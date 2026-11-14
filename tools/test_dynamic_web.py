"""Dynamic fallback routing, failure durability, network isolation and budgets."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, enrich, extract, notion, pipeline, render_web, storage, store, work_budget
import staging_stub

URL = 'https://robodojo-benchmark.com/'
FIXTURES = Path(__file__).parent / 'fixtures' / 'dynamic_web'
SHELL = '<html><head><title>Demo</title><script type="module" src="/main.js"></script></head><body><div id="root"></div></body></html>'
CONTENT = '<html><head><title>Demo</title></head><body><main><h1>Robot benchmark</h1><p>Robot policies are evaluated in simulation and real environments.</p><table><tr><th>Policy</th><th>Success</th></tr><tr><td>Pi0</td><td>72%</td></tr></table></main></body></html>'


def response(html, code=200):
    return httpx.Response(code, text=html, request=httpx.Request('GET', URL))


class DynamicTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(extract, 'parsehub_platform', return_value=None),
                        patch.object(config, 'DYNAMIC_WEB_ENABLED', True)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_short_article_never_uses_browser(self):
        short = (Path(__file__).parent / 'fixtures/web_capture/sivers-short-article.html').read_text()
        with patch.object(extract.httpx, 'get', return_value=response(short)), patch.object(render_web, 'render') as browser:
            art = pipeline.fetch_for(URL)
        self.assertTrue(art['ok'])
        browser.assert_not_called()

    def test_dynamic_shell_recovers_with_table_headers(self):
        with patch.object(extract.httpx, 'get', return_value=response(SHELL)), patch.object(render_web, 'render',
                return_value={'ok': True, 'html': CONTENT, 'url': URL}) as browser:
            art = pipeline.fetch_for(URL)
        self.assertTrue(art['ok'])
        self.assertEqual(art['fetch_path'], 'rendered')
        self.assertIn('| Policy | Success |', art['text'])
        self.assertIn('72%', art['text'])
        browser.assert_called_once()

    def test_rendered_route_metadata_overrides_stale_site_shell_title(self):
        html = CONTENT.replace('<title>Demo</title>', '<title>Site home</title>'
            '<meta property="og:title" content="Site home">'
            '<meta property="og:title" content="Robot evaluation report">')
        art = extract.parse_article_html(html, URL + 'report/evaluation')
        self.assertTrue(art['ok'])
        self.assertEqual(art['title'], 'Robot evaluation report')

    def test_gates_http_errors_empty_pages_and_special_paths_never_render(self):
        gate = (Path(__file__).parent / 'fixtures/web_capture/eaa-member-gate.html').read_text()
        for html, code in [(gate, 200), (SHELL, 403), ('<html><body></body></html>', 200),
                           (SHELL.replace('Demo', 'Just a moment...'), 200)]:
            with self.subTest(code=code, html=html[:30]), patch.object(extract.httpx, 'get', return_value=response(html, code)), patch.object(render_web, 'render') as browser:
                art = pipeline.fetch_for(URL)
                self.assertFalse(art['ok'])
                browser.assert_not_called()
        for u in ['https://arxiv.org/abs/2601.00001', 'https://doi.org/10.1234/abcd', 'https://chatgpt.com/share/x', 'https://youtu.be/example']:
            with patch.object(extract.httpx, 'get', return_value=response(SHELL)), patch.object(render_web, 'render') as browser:
                pipeline.fetch_for(u)
                browser.assert_not_called()

    def test_loading_placeholder_must_not_be_saved(self):
        html = SHELL.replace('<div id="root"></div>', '<div id="root"><p>Loading...</p></div>')
        self.assertEqual(extract.parse_article_html(html, URL)['failure_kind'], 'dynamic_shell')
        with patch.object(extract.httpx, 'get', return_value=response(SHELL)), patch.object(render_web, 'render',
                return_value={'ok': True, 'html': html, 'url': URL}):
            self.assertFalse(pipeline.fetch_for(URL)['ok'])

    def test_title_plus_loading_routes_to_browser_and_never_to_model(self):
        for placeholder in ['Loading…', 'Loading...', 'Please wait', '加载中…', '正在加载。', '']:
            html = SHELL.replace('<div id="root"></div>', f'<div id="root"><main><h1>Robot <span>Benchmark</span></h1><p>{placeholder}</p></main></div>')
            with self.subTest(placeholder=placeholder):
                self.assertEqual(extract.parse_article_html(html, URL)['failure_kind'], 'dynamic_shell')
                with patch.object(extract.httpx, 'get', return_value=response(html)), patch.object(render_web, 'render', return_value={'ok': True, 'html': CONTENT, 'url': URL}) as browser:
                    self.assertTrue(pipeline.fetch_for(URL)['ok'])
                    browser.assert_called_once()
        staged = staging_stub.install(store)
        with patch.object(store, 'find_by_url', return_value=None), patch.object(store, 'pending_url_owner', return_value=None), patch.object(extract.httpx, 'get', return_value=response(html)), patch.object(render_web, 'render', return_value={'ok': False, 'failure_kind': 'render_timeout', 'error': 'timeout'}), patch.object(enrich, 'enrich') as llm, patch.object(pipeline, '_create_material') as material:
            pipeline._handle_url({'chat': {'id': 1}, 'message_id': 2}, URL, '')
        self.assertTrue(staged)
        llm.assert_not_called()
        material.assert_not_called()

    def test_short_loading_article_remains_valid(self):
        html = '<html><head><title>Loading</title></head><body><article><h1>Loading</h1><p>Loading a model once saves time.</p></article></body></html>'
        self.assertTrue(extract.parse_article_html(html, URL)['ok'])

    def test_browser_waits_past_stable_placeholder_and_times_out_if_unresolved(self):
        for resolves in [False, True]:
            clock = [0.0]
            page = Mock()
            page.evaluate.side_effect = lambda _: {'text': 'Robot BenchmarkLoading…' if not resolves or clock[0] < 2 else 'Robot BenchmarkActual results are available.', 'headings': ['Robot Benchmark']}
            page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
            page.content.return_value = CONTENT
            with patch.object(render_web.time, 'monotonic', side_effect=lambda: clock[0]):
                if resolves:
                    self.assertEqual(render_web._wait_content(page, 5, []), CONTENT)
                    self.assertGreaterEqual(clock[0], 3.2)
                    page.content.assert_called_once()
                else:
                    with self.assertRaises(render_web.FetchFailure) as error:
                        render_web._wait_content(page, 5, [])
                    self.assertEqual(error.exception.kind, 'render_timeout')
                    page.content.assert_not_called()

    def test_disabled_and_insufficient_budget_keep_shell_staged(self):
        for enabled, seconds in [(False, 210), (True, 100)]:
            with patch.object(config, 'DYNAMIC_WEB_ENABLED', enabled), work_budget.limit(seconds), patch.object(extract.httpx, 'get', return_value=response(SHELL)), patch.object(render_web, 'render') as browser:
                self.assertFalse(pipeline.fetch_for(URL)['ok'])
                browser.assert_not_called()
        with work_budget.limit(150), patch.object(extract.httpx, 'get', return_value=response(SHELL)), patch.object(render_web, 'render', return_value={'ok': False, 'failure_kind': 'render_timeout', 'error': 'timeout'}) as browser:
            pipeline.fetch_for(URL)
            self.assertLessEqual(browser.call_args.kwargs['timeout'], 30)

    def test_render_failure_never_calls_model_or_creates_material(self):
        staged = staging_stub.install(store)
        with patch.object(store, 'find_by_url', return_value=None), patch.object(store, 'pending_url_owner', return_value=None), patch.object(extract.httpx, 'get', return_value=response(SHELL)), patch.object(render_web, 'render',
                return_value={'ok': False, 'failure_kind': 'render_timeout', 'error': '加载超时'}), patch.object(enrich, 'enrich') as llm, patch.object(pipeline, '_create_material') as material:
            pipeline._handle_url({'chat': {'id': 1}, 'message_id': 2}, URL, '')
        self.assertTrue(staged)
        llm.assert_not_called()
        material.assert_not_called()

    def test_retry_recovers_same_message_and_thought_once_then_dedupes(self):
        staged = staging_stub.install(store)
        store.stage_url(URL, 1, 2, 'dynamic shell', ['thought'])
        mappings = {}
        def create(**kw):
            self.assertEqual(kw['messages'][0]['message_id'], 2)
            mappings[URL] = {'notion_page_id': 'material', 'notion_url': 'https://notion.so/material'}
            return {'id': 'material', 'url': 'https://notion.so/material'}
        with patch.object(store, 'find_by_url', side_effect=lambda u: mappings.get(u)), patch.object(store, 'pending_url_owner', return_value=None), patch.object(notion, 'page_alive', return_value=True), patch.object(extract.httpx, 'get', return_value=response(SHELL)), patch.object(render_web, 'render', return_value={'ok': True, 'html': CONTENT, 'url': URL}), patch.object(storage, 'archive_text', return_value='gs://body'), patch.object(enrich, 'enrich', return_value=(enrich.Enrichment(title='Demo', overview='概览', key_points=['要点'], material_type='网页'), '')), patch.object(pipeline, '_create_material', side_effect=create) as material, patch.object(notion, 'link_thought_to_material') as relate:
            self.assertTrue(pipeline._retry_staged(next(iter(staged.values())), lambda _: None)[0])
            self.assertFalse(staged)
            relate.assert_not_called()  # A legacy same-message checkpoint is not a reply.
            pipeline._handle_url({'chat': {'id': 1}, 'message_id': 2}, URL, '')
            material.assert_called_once()

    def test_cloud_capture_has_real_body_tables_and_cover_candidates(self):
        if not (FIXTURES / 'robodojo-rendered.html').exists():
            self.skipTest('Cloud probe capture not yet available')
        raw = (FIXTURES / 'robodojo-shell.html').read_text()
        rendered = (FIXTURES / 'robodojo-rendered.html').read_text()
        self.assertEqual(extract.parse_article_html(raw, URL)['failure_kind'], 'dynamic_shell')
        for _ in range(4):  # A parser cache must not reject later retries.
            art = extract.parse_article_html(rendered, URL)
            self.assertTrue(art['ok'])
            self.assertIn('RoboDojo', art['title'])
            self.assertIn('|', art['text'])
            self.assertIn('| Rank | Model | Score | SR | Gen | Precision | Long-Horizon | Memory | Open |', art['text'])
            self.assertIn('PhysicalRSI', art['text'])
            self.assertTrue(art['art_candidates']['shares'] or art['art_candidates']['body'])


class BrowserLimits(unittest.TestCase):
    def stats(self):
        return {"requests": 0, "fetched": 0, "skipped": 0, "bytes": 0, "blocked": 0}

    def test_media_heavy_report_keeps_network_budget_for_article_data(self):
        stats = self.stats()
        with patch.object(render_web, '_resource', return_value=(200, {}, b'article data')) as fetch:
            for i in range(400):
                route = Mock()
                request = Mock(resource_type=['image', 'media', 'font'][i % 3])
                render_web._route_resource(route, request, time.monotonic() + 10, {}, stats)
                route.abort.assert_called_once()
                route.fulfill.assert_not_called()
            fetch.assert_not_called()
            route = Mock()
            render_web._route_resource(route, Mock(resource_type='fetch', redirected_from=None),
                                       time.monotonic() + 10, {}, stats)
            route.fulfill.assert_called_once()
        self.assertEqual(stats['requests'], 401)
        self.assertEqual(stats['skipped'], 400)
        self.assertEqual(stats['fetched'], 1)
        self.assertEqual(stats['bytes'], len(b'article data'))

    def test_actual_network_requests_and_intercepted_requests_still_bounded(self):
        for resource_type, limit, counter in [('script', render_web.MAX_REQUESTS, 'fetched'),
                                              ('image', render_web.MAX_INTERCEPTED_REQUESTS, 'requests')]:
            stats = self.stats()
            stats[counter] = limit
            with self.subTest(resource_type=resource_type), patch.object(render_web, '_resource') as fetch:
                with self.assertRaises(render_web.FetchFailure) as error:
                    render_web._route_resource(Mock(), Mock(resource_type=resource_type),
                                               time.monotonic() + 10, {}, stats)
                self.assertEqual(error.exception.kind, 'resource_limit')
                fetch.assert_not_called()

    def test_network_timeout_and_total_bytes_remain_bounded(self):
        request = Mock(resource_type='fetch', redirected_from=None)
        with patch.object(render_web, '_resource') as fetch:
            with self.assertRaises(render_web.FetchFailure) as error:
                render_web._route_resource(Mock(), request, time.monotonic() - 1, {}, self.stats())
            self.assertEqual(error.exception.kind, 'render_timeout')
            fetch.assert_not_called()
        stats = self.stats()
        stats['bytes'] = render_web.MAX_TOTAL_BYTES
        route = Mock()
        with patch.object(render_web, '_resource', return_value=(200, {}, b'x')):
            with self.assertRaises(render_web.FetchFailure):
                render_web._route_resource(route, request, time.monotonic() + 10, {}, stats)
            route.fulfill.assert_not_called()

    def test_private_addresses_credentials_and_dns_rebinding_rejected(self):
        for u in ['file:///etc/passwd', 'http://localhost', 'http://metadata.google.internal', 'http://user:pw@example.org', 'http://127.0.0.1', 'http://169.254.169.254', 'http://[::1]', 'http://[::ffff:127.0.0.1]', 'http://10.0.0.1']:
            with self.subTest(url=u), self.assertRaises(render_web.FetchFailure):
                render_web.public_target(u, resolver=lambda *a, **k: [(None, None, None, None, ('127.0.0.1', 80))])
        resolver = lambda *a, **k: [(None, None, None, None, (ip, 443)) for ip in ['8.8.8.8', '10.0.0.1']]
        with self.assertRaises(render_web.FetchFailure):
            render_web.public_target('https://public.example', resolver=resolver)

    def test_fetch_pins_validated_ip_preserving_tls_host_and_does_not_follow_redirect(self):
        req = Mock(url='https://public.example/body?x=1', headers={}, method='GET', post_data_buffer=None)
        reply = Mock(status=302)
        reply.read.return_value = b''
        reply.getheaders.return_value = [('Location', 'http://169.254.169.254/')]
        conn = Mock()
        conn.getresponse.return_value = reply
        targets = {('https', 'public.example', None): (render_web.urlsplit(req.url), 'public.example', 443, '8.8.8.8')}
        with patch.object(render_web, 'PinnedHTTPS', return_value=conn) as transport, patch.object(render_web, 'public_target') as resolve:
            status, _, _ = render_web._resource(req, 35, targets)
        self.assertEqual(status, 302)
        transport.assert_called_once_with('public.example', '8.8.8.8', 443, 8)
        self.assertEqual(conn.request.call_args.kwargs['headers']['Host'], 'public.example')
        resolve.assert_not_called()

    def test_one_slot_busy_returns_without_spawning(self):
        render_web._slot.acquire()
        try:
            with patch.object(render_web.subprocess, 'Popen') as proc:
                self.assertEqual(render_web.render(URL, wait_timeout=.01)['failure_kind'], 'render_busy')
                proc.assert_not_called()
        finally:
            render_web._slot.release()

    def test_timeout_kills_entire_process_group_and_releases_slot(self):
        real_popen = subprocess.Popen
        children = []
        def slow(*args, **kwargs):
            proc = real_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            children.append(proc)
            return proc
        with patch.object(render_web.subprocess, 'Popen', side_effect=slow), patch.object(render_web.os, 'killpg', wraps=os.killpg) as kill:
            out = render_web.render(URL, timeout=3)
        self.assertEqual(out['failure_kind'], 'render_timeout')
        kill.assert_called_once_with(children[0].pid, signal.SIGKILL)
        self.assertEqual(children[0].poll(), -signal.SIGKILL)
        self.assertTrue(render_web._slot.acquire(blocking=False))
        render_web._slot.release()

    def test_nested_deadline_never_extends_task_and_model_reserves_write_time(self):
        with work_budget.limit(100):
            with work_budget.limit(260):
                self.assertLessEqual(work_budget.remaining(), 100)
                self.assertLessEqual(work_budget.timeout(60, reserve=60), 40)
        self.assertEqual(work_budget.remaining(), float('inf'))


if __name__ == '__main__':
    unittest.main()
