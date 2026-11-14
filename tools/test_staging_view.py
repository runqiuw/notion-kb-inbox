"""Read-only staging report, chat isolation, current failures and command routing."""
import asyncio
import copy
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, main, pipeline, staging_ui, store, telegram
from test_tag_management import DB
from tools.set_webhook import COMMANDS


class Tests(unittest.TestCase):
    def setUp(self):
        self.db = DB()
        self.patcher = patch.object(store, 'db', lambda: self.db)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def update(self, key, status, text='', mid=1, chat=7, **kw):
        self.db.data['updates/' + str(key)] = {'status': status, 'attempts': 4, 'received_at': 1700000000,
                                             'payload': {'message': {'chat': {'id': chat}, 'message_id': mid, 'text': text}}, **kw}

    def test_empty_and_invalid_arguments_are_read_only(self):
        self.assertIn('暂存区为空', staging_ui.report(7))
        for args in (['0'], ['-1'], ['x'], ['2', 'x'], ['9' * 100], ['１２']):
            self.db.fail = True
            self.assertIn('用法', staging_ui.report(7, args))
        self.assertEqual(self.db.data, {})

    def test_content_reasons_scope_dedup_and_current_status(self):
        self.db.data['staged/a'] = {'url': 'https://example.test/a', 'chat_id': 7, 'message_id': 1,
                                    'error': '抓取 403', 'attempts': 3, 'last_at': 1700000000}
        self.db.data['staged/other'] = {'url': 'https://private.test', 'chat_id': 8, 'error': 'private'}
        self.update(1, 'failed', 'https://example.test/a', error='same failed URL')
        self.update(2, 'waiting_llm', '等待的想法', mid=2)
        self.update(3, 'failed', '失败的内容 <x>', mid=3, error='解析失败')
        self.update(4, 'done', '已完成', mid=4)
        self.update(5, 'failed', 'other user', mid=5, chat=8)
        self.update(6, 'failed', '/tags', mid=6)
        before = copy.deepcopy(self.db.data)
        text = staging_ui.report(7)
        self.assertIn('共 3 条', text)
        for value in ('https://example.test/a', '抓取 403', '尝试 3 次', '等待的想法', '失败的内容 <x>', '解析失败'):
            self.assertIn(value, text)
        for value in ('private', 'other user', '已完成', 'same failed URL', '/tags'):
            self.assertNotIn(value, text)
        self.assertEqual(self.db.data, before)
        self.db.data['updates/3']['status'] = 'done'
        self.assertNotIn('失败的内容', staging_ui.report(7))

    def test_pagination_and_telegram_utf16_length(self):
        for i in range(9):
            self.db.data['staged/' + str(i)] = {'url': 'https://example.test/' + str(i) + '😀' * 900,
                                               'chat_id': 7, 'message_id': i + 1, 'error': '😀' * 900,
                                               'attempts': 1, 'last_at': i + 1700000000}
        for page in range(1, 4):
            text = staging_ui.report(7, [str(page)])
            self.assertIn(f'第 {page}/3 页', text)
            self.assertLess(len(text.encode('utf-16-le')) // 2, 4096)
        self.assertIn('下一页：/staged 2', staging_ui.report(7))
        self.assertNotIn('下一页', staging_ui.report(7, ['999']))
        self.assertIn('第 3/3 页', staging_ui.report(7, ['999']))

    def test_failed_file_latest_message_and_secret_redaction(self):
        self.update(1, 'failed', 'old')
        self.db.data['updates/1'].update(latest_message={'chat': {'id': 7}, 'message_id': 1,
                                                         'document': {'file_name': 'paper.pdf'}},
                                        error='key sk-long-secret123 bot123:abcdefghi')
        with patch.object(config, 'LLM_API_KEY', 'sk-long-secret123'):
            text = staging_ui.report(7)
        self.assertIn('paper.pdf', text)
        self.assertNotIn('old', text)
        self.assertNotIn('sk-long-secret123', text)
        self.assertNotIn('bot123:abcdefghi', text)

    def test_pipeline_route_and_menu(self):
        with patch.object(staging_ui, 'report', return_value='内容 <x>') as report, \
             patch('app.tag_ui.handle_reply', return_value=False):
            text, url = pipeline.dispatch({'chat': {'id': 7}, 'text': '/staged@bot 2'}, False)
        report.assert_called_once_with(7, ['2'])
        self.assertEqual((text, url), ('内容 &lt;x&gt;', ''))
        self.assertIn('/staged', pipeline.HELP)
        self.assertIn('/staged', [c for c, _ in COMMANDS])

    def test_webhook_is_read_only_and_owner_restricted(self):
        class Request:
            async def json(self):
                return {'update_id': 20, 'message': {'chat': {'id': 7}, 'from': {'id': 7},
                                                    'message_id': 10, 'text': '/staged@bot 2'}}
        with patch.object(config, 'TELEGRAM_ALLOWED_USER_ID', '7'), \
             patch.object(config, 'TELEGRAM_WEBHOOK_SECRET', ''), \
             patch.object(staging_ui, 'report', return_value='stage contents') as report, \
             patch.object(telegram, 'send', return_value={'message_id': 100}) as send, \
             patch.object(store, 'save_update', side_effect=AssertionError('must not enqueue read-only command')):
            self.assertEqual(asyncio.run(main.webhook(Request(), '')), {'ok': True, 'skipped': 'command'})
            report.assert_called_once_with(7, ['2'])
            send.assert_called_once_with(7, 'stage contents', reply_to=10)
            with patch.object(config, 'TELEGRAM_ALLOWED_USER_ID', '8'):
                self.assertEqual(asyncio.run(main.webhook(Request(), ''))['skipped'], 'not allowed')
            self.assertEqual(report.call_count, 1)

    def test_webhook_read_failure_is_explained(self):
        class Request:
            async def json(self):
                return {'update_id': 20, 'message': {'chat': {'id': 7}, 'from': {'id': 7},
                                                    'message_id': 10, 'text': '/staged'}}
        with patch.object(config, 'TELEGRAM_ALLOWED_USER_ID', '7'), \
             patch.object(config, 'TELEGRAM_WEBHOOK_SECRET', ''), \
             patch.object(staging_ui, 'report', side_effect=RuntimeError('database offline')), \
             patch.object(telegram, 'send') as send:
            asyncio.run(main.webhook(Request(), ''))
        self.assertIn('读取失败', send.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
