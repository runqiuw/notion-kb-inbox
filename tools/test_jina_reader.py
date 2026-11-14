"""Anonymous fallback identity, access gates, bounded reads and ingest routing."""
from contextlib import contextmanager
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from app import config, enrich, extract, jina_reader, notion, pipeline, storage, store, work_budget

URL = 'https://zhuanlan.zhihu.com/p/123'
BODY = ('这里讨论推理系统的吞吐与延迟，并解释不同并行策略、缓存和负载均衡的适用条件。' * 5
        + '\n\n## 方法\n\n[更多细节](https://zhida.zhihu.com/search?zd_token=tracking)'
        + '\n\n![图](https://pic1.zhimg.com/diagram.jpg)'
        + '\n\n![重复](https://pic1.zhimg.com/diagram.jpg)'
        + '\n\n|指标|数值|\n|---|---|\n|延迟|12|\n\n## 参考\n\n结束。')


def response(body=BODY, title='原始文章标题', source=URL, warning=''):
    return f'Title: {title}\n\nURL Source: {source}\n\n{warning}Markdown Content:\n{body}'


class Reader(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.raw = response()
        self.status = 200
        self.p(config, 'JINA_READER_ENABLED', True)
        self.p(extract, 'parsehub_platform', lambda u: 'ZhihuParser')
        self.p(extract, 'parse_via_parsehub', lambda *a: {'ok': False, 'error': 'native 403'})
        self.p(jina_reader.httpx, 'stream', self.stream)
        self.p(storage, 'archive', lambda *a: 'gs://test/reader.md')

    def p(self, obj, name, value):
        p = patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)
        return value

    @contextmanager
    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        yield httpx.Response(self.status, content=self.raw.encode(), request=httpx.Request(method, url))

    def test_fallback_text_images_exact_archive_and_anonymous_request(self):
        saved = []
        self.p(storage, 'archive', lambda *a: saved.append(a) or 'gs://test/raw.md')
        with patch.dict(os.environ, {'JINA_API_KEY': 'never-forward', 'ZHIHU_DC0': 'never-forward'}):
            art = pipeline.fetch_for(URL + '?utm_source=tracking#section')
        self.assertTrue(art['ok'])
        self.assertEqual(art['title'], '原始文章标题')
        self.assertEqual(art['images'], ['https://pic1.zhimg.com/diagram.jpg'])
        self.assertIn('结束。', art['text'])
        self.assertNotIn('zd_token', art['text'])
        self.assertEqual(saved[0][0], self.raw.encode())
        self.assertNotIn('reader_response', art)
        self.assertEqual(art['capture']['raw_archive'], 'gs://test/raw.md')
        method, url, args = self.calls[0]
        self.assertEqual(url, jina_reader.ENDPOINT + URL)
        self.assertEqual(args['headers'], {'Accept': 'text/plain'})
        self.assertFalse(args['follow_redirects'])
        self.assertEqual(len(self.calls), 1)

    def test_native_success_never_calls_reader(self):
        self.p(extract, 'parse_via_parsehub', lambda *a: {'ok': True, 'title': 'native', 'text': '已取得正文'})
        self.assertEqual(pipeline.fetch_for(URL)['title'], 'native')
        self.assertFalse(self.calls)

    def test_switch_disables_fallback(self):
        self.p(config, 'JINA_READER_ENABLED', False)
        self.assertFalse(pipeline.fetch_for(URL)['ok'])
        self.assertFalse(self.calls)

    def test_scope_does_not_forward_accounts_questions_or_other_hosts(self):
        for url in ['https://www.zhihu.com/question/123', 'https://zhihu.com.evil/p/123',
                    'https://evilzhihu.com/p/123', 'https://user:secret@zhuanlan.zhihu.com/p/123',
                    'https://zhuanlan.zhihu.com:8080/p/123', 'https://mp.weixin.qq.com/s/123']:
            with self.subTest(url=url):
                self.assertFalse(jina_reader.fetch(url)['ok'])
        self.assertFalse(self.calls)
        self.assertTrue(jina_reader.canonical_url('https://www.zhihu.com/question/1/answer/2'))

    def test_original_source_must_match_even_for_substantive_body(self):
        self.raw = response(source='https://zhuanlan.zhihu.com/p/456')
        self.assertFalse(pipeline.fetch_for(URL)['ok'])

    def test_envelope_and_title_are_required(self):
        for raw in [BODY, response(title=''), response(source='https://example.com/p/123')]:
            with self.subTest(raw=raw[:60]):
                self.raw = raw
                self.assertFalse(jina_reader.fetch(URL)['ok'])

    def test_gates_partial_pages_and_title_only_are_not_articles(self):
        for body, title, warning in [
            ('环境异常，完成验证后即可继续访问。' * 20, '访问提示', ''),
            ('请您登录后查看更多专业优质内容。' * 20, '访问提示', ''),
            ('知乎原始文章标题', '知乎原始文章标题', ''),
            (BODY, '知乎', ''),
            (BODY, '正文', 'Warning: Target URL returned error 403: Forbidden\n'),
            (BODY, '正文', 'Warning: This page is not fully loaded\n'),
        ]:
            with self.subTest(title=title, warning=warning):
                self.raw = response(body, title, warning=warning)
                self.assertFalse(jina_reader.fetch(URL)['ok'])

    def test_article_can_discuss_login(self):
        self.raw = response(BODY + '\n\n本文也讨论验证码登录与权限设计。')
        self.assertTrue(jina_reader.fetch(URL)['ok'])

    def test_unhandled_images_do_not_silently_disappear(self):
        self.raw = response(BODY + '\n\n![missing](http://127.0.0.1/private)')
        self.assertFalse(jina_reader.fetch(URL)['ok'])

    def test_limits_and_archive_failure_keep_native_error(self):
        for status in [429, 403, 503]:
            self.status = status
            art = pipeline.fetch_for(URL)
            self.assertFalse(art['ok'])
            self.assertIn('native 403', art['error'])
            self.assertIn(str(status), art['error'])
        self.status = 200
        self.p(storage, 'archive', lambda *a: '')
        self.assertIn('归档失败', pipeline.fetch_for(URL)['error'])

    def test_body_size_and_remaining_budget_are_bounded(self):
        self.raw = 'x' * (jina_reader.MAX_RESPONSE_BYTES + 1)
        self.assertFalse(jina_reader.fetch(URL)['ok'])
        self.calls.clear()
        with work_budget.limit(1):
            self.assertFalse(jina_reader.fetch(URL)['ok'])
        self.assertFalse(self.calls)

    def test_unarchived_image_stages_before_model_or_publication(self):
        self.p(config, 'GCP_PROJECT', '')
        self.p(store, 'pending_url_owner', lambda u: None)
        self.p(store, 'find_by_url', lambda u: None)
        self.p(pipeline.comments, 'capture', lambda *a: {'status': 'unavailable', 'error': '受限'})
        self.p(storage, 'archive_text', lambda *a: 'gs://test/body.txt')
        self.p(pipeline, '_read_url_images', lambda *a: ([b'image'], [''], []))
        staged = []
        self.p(pipeline, '_stage', lambda *a, **kw: staged.append(a) or ('staged', ''))
        self.p(enrich, 'enrich', lambda *a, **kw: self.fail('unarchived image reached model'))
        result = pipeline._handle_url({'chat': {'id': 1}, 'message_id': 1}, URL, '')
        self.assertEqual(result, ('staged', ''))
        self.assertIn('未全部归档', staged[0][2])

    def test_ingest_and_page_retry_preserve_body_provenance_and_comment_failure(self):
        self.raw = response(BODY.replace('![图](https://pic1.zhimg.com/diagram.jpg)', '')
                            .replace('![重复](https://pic1.zhimg.com/diagram.jpg)', ''))
        self.p(config, 'GCP_PROJECT', '')
        self.p(store, 'pending_url_owner', lambda u: None)
        self.p(store, 'find_by_url', lambda u: None)
        self.p(pipeline.comments, 'capture', lambda *a: {'status': 'unavailable', 'error': '评论接口受限'})
        self.p(enrich, 'enrich', lambda *a, **kw: (enrich.Enrichment(title='AI标题', overview='中文概览'), ''))
        self.p(storage, 'archive_text', lambda *a: 'gs://test/body.txt')
        made = []
        self.p(pipeline, '_create_material', lambda **kw: made.append(kw) or {'id': 'page', 'url': 'https://notion/page'})
        self.p(pipeline, '_claim_staged', lambda *a: None)
        _, url = pipeline._handle_url({'chat': {'id': 1}, 'message_id': 1}, URL, '')
        self.assertEqual(url, 'https://notion/page')
        for value in ['Jina Reader', 'gs://test/reader.md', '结束。', '评论未采集']:
            self.assertIn(value, str(made[0]['blocks']))
        page = {'id': 'page', 'properties': {'标题': {'type': 'title', 'title': [{'plain_text': '原始标题'}]},
                                           '原始链接': {'type': 'url', 'url': URL}}}
        blocks = []
        self.p(notion, 'append_blocks', lambda pid, values: blocks.extend(values))
        self.p(notion, 'refresh_machine_fields', lambda *a, **kw: None)
        ok, _ = pipeline._retry_one(page, lambda s: None)
        self.assertTrue(ok)
        self.assertIn('Jina Reader', str(blocks))
        self.assertIn('结束。', str(blocks))


if __name__ == '__main__':
    unittest.main()
