"""A delayed Telegram acknowledgement must not hold up menu rendering."""
import asyncio
import os
import sys
import threading
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, main, tag_ui, telegram


class Tests(unittest.TestCase):
    def test_tags_ack_does_not_reply_to_command(self):
        class Request:
            def __init__(self, text):
                self.text = text
            async def json(self):
                return {'update_id': 1, 'message': {'chat': {'id': 7}, 'from': {'id': 7},
                                                   'message_id': 42, 'text': self.text}}
        with patch.object(config, 'TELEGRAM_ALLOWED_USER_ID', '7'), \
             patch.object(config, 'TELEGRAM_WEBHOOK_SECRET', ''), \
             patch.object(main.store, 'save_update', return_value=True), \
             patch.object(main, '_record_ack'), \
             patch.object(main.tasks, 'enqueue_process', return_value=True), \
             patch.object(telegram, 'send', return_value={'message_id': 500}) as send:
            asyncio.run(main.webhook(Request('/tags@bot'), ''))
            self.assertIsNone(send.call_args.kwargs.get('reply_to'))
            asyncio.run(main.webhook(Request('普通想法'), ''))
            self.assertEqual(send.call_args.kwargs['reply_to'], 42)

    def test_webhook_renders_while_acknowledgement_is_waiting(self):
        ack_started, rendered = threading.Event(), threading.Event()
        callback = {'id': 'test', 'data': 'tg:nonce:page:1', 'from': {'id': 7}}
        class Request:
            async def json(self):
                return {'update_id': 1, 'callback_query': callback}
        def acknowledge(cid):
            self.assertEqual(cid, 'test')
            ack_started.set()
            self.assertTrue(rendered.wait(3), 'menu waited for acknowledgement')
        def render(cb, *, acknowledge):
            self.assertFalse(acknowledge, 'do not acknowledge twice')
            self.assertEqual(cb, callback)
            self.assertTrue(ack_started.wait(3))
            rendered.set()
        with patch.object(config, 'TELEGRAM_ALLOWED_USER_ID', '7'), \
             patch.object(config, 'TELEGRAM_WEBHOOK_SECRET', ''), \
             patch.object(telegram, 'answer_callback', acknowledge), \
             patch.object(tag_ui, 'handle_callback', render):
            result = asyncio.run(main.webhook(Request(), ''))
        self.assertEqual(result, {'ok': True})

    def test_acknowledgement_timeout_is_short_and_failure_is_best_effort(self):
        with patch.object(telegram.httpx, 'post', side_effect=httpx.ReadTimeout('delayed')) as post:
            telegram.answer_callback('test')
        self.assertEqual(post.call_args.kwargs['timeout'], 2.0)
        self.assertNotIn('request_timeout', post.call_args.kwargs['json'])

    def test_page_edit_keeps_normal_timeout(self):
        response = httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}},
                                  request=httpx.Request('POST', 'https://telegram.test'))
        with patch.object(telegram.httpx, 'post', return_value=response) as post:
            self.assertEqual(telegram.edit(7, 1, 'page')['message_id'], 1)
        self.assertEqual(post.call_args.kwargs['timeout'], config.FETCH_TIMEOUT)


if __name__ == '__main__':
    unittest.main()
