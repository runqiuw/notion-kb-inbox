"""Cancel the armed note through the webhook without affecting other entries."""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, main, notion, pipeline, store, telegram
from test_park_expiry import Database


PAGE = '01234567-89ab-cdef-0123-456789abcdef'
START = '/start n' + PAGE.replace('-', '')


class CancelTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.patch(store, '_db', self.db)
        self.patch(store.firestore, 'transactional', lambda fn: fn)
        self.send = self.patch(telegram, 'send', return_value={'message_id': 10})
        self.edit = self.patch(telegram, 'edit')
        self.ack = self.patch(telegram, 'answer_callback')
        self.delete = self.patch(telegram, 'delete')
        self.patch(config, 'TELEGRAM_ALLOWED_USER_ID', '7')
        self.patch(config, 'TELEGRAM_WEBHOOK_SECRET', '')
        self.patch(notion, 'get_page', return_value={
            'id': PAGE, 'url': 'https://notion.so/' + PAGE,
            'properties': {'标题': {'type': 'title', 'title': [{'plain_text': 'A < B & C'}]}}})
        self.create = self.patch(notion, 'create_thought', return_value={
            'id': 'note', 'url': 'https://notion.so/note'})
        self.patch(notion, 'body_location', return_value=('note', ['body']))

    def patch(self, obj, name, *args, **kwargs):
        p = patch.object(obj, name, *args, **kwargs)
        result = p.start()
        self.addCleanup(p.stop)
        return result

    def message(self, text=START, mid=1, chat=7):
        return {'chat': {'id': chat}, 'from': {'id': 7}, 'message_id': mid, 'text': text}

    def arm(self):
        self.assertEqual(pipeline.dispatch(self.message(), False), ('', ''))
        prompt = self.send.call_args
        data = prompt.kwargs['markup']['inline_keyboard'][-1][0]['callback_data']
        self.assertLessEqual(len(data.encode()), telegram.CALLBACK_DATA_LIMIT)
        return data

    def callback(self, data, chat=7, user=7):
        return {'id': 'callback', 'from': {'id': user}, 'data': data,
                'message': {'chat': {'id': chat}, 'message_id': 10}}

    def webhook(self, cb):
        class Request:
            async def json(self):
                return {'update_id': 100, 'callback_query': cb}
        return asyncio.run(main.webhook(Request(), ''))

    def test_prompt_has_cancel_and_notion_buttons_and_escaped_title(self):
        self.arm()
        prompt = self.send.call_args
        self.assertIn('A &lt; B &amp; C', prompt.args[1])
        self.assertEqual(prompt.kwargs['reply_to'], 1)
        self.assertEqual(prompt.kwargs['parse_mode'], 'HTML')
        buttons = prompt.kwargs['markup']['inline_keyboard']
        self.assertEqual(buttons[0][0]['url'], 'https://notion.so/' + PAGE)
        self.assertEqual(buttons[-1][0]['text'], '取消')

    def test_worker_clears_processing_ack_without_overwriting_buttons(self):
        store.save_update(1, {'message': self.message()})
        self.db.data['updates']['1']['ack_message_id'] = 500
        main._process(1)
        self.assertEqual(store.get_update(1)['status'], 'done')
        self.send.assert_called_once()
        self.delete.assert_called_once_with(7, 500)
        self.edit.assert_not_called()
        self.assertTrue(self.db.data['pending']['7']['token'])

    def test_webhook_cancel_then_next_text_is_standalone(self):
        data = self.arm()
        with patch.object(main.tasks, 'enqueue_process') as enqueue, \
                patch.object(store, 'save_update') as save:
            self.assertEqual(self.webhook(self.callback(data)), {'ok': True})
        enqueue.assert_not_called()
        save.assert_not_called()
        self.assertNotIn('7', self.db.data['pending'])
        self.assertEqual(self.edit.call_args.kwargs['markup'], telegram.keyboard([]))
        self.assertIn('已取消', self.edit.call_args.args[2])
        self.create.assert_not_called()
        pipeline.dispatch(self.message('独立的新想法', mid=2), False)
        self.assertEqual(self.create.call_args.kwargs['related'], [])
        self.assertEqual(self.create.call_args.kwargs['note_type'], '随想')
        self.assertEqual(store.get_record(7, 2)['text'], '独立的新想法')

    def test_old_button_cannot_cancel_new_prompt_for_the_same_material(self):
        old = self.arm()
        new = self.arm()
        self.assertNotEqual(old, new)
        pipeline.handle_callback(self.callback(old))
        self.assertEqual(self.db.data['pending']['7']['token'], new[3:])
        self.assertNotIn('已取消', self.edit.call_args.args[2])
        self.assertTrue(store.cancel_pending_note(7, new[3:]))

    def test_other_chat_button_cannot_cancel_this_chat(self):
        data = self.arm()
        pipeline.handle_callback(self.callback(data, chat=8))
        self.assertEqual(store.take_pending_note(7), PAGE)

    def test_unauthorized_callback_leaves_pending_state_untouched(self):
        data = self.arm()
        self.assertEqual(self.webhook(self.callback(data, user=8))['skipped'], 'not allowed')
        self.assertEqual(store.take_pending_note(7), PAGE)
        self.edit.assert_not_called()

    def test_repeated_cancel_and_cancel_after_consumption_are_safe(self):
        token = self.arm()[3:]
        self.assertTrue(store.cancel_pending_note(7, token))
        self.assertFalse(store.cancel_pending_note(7, token))
        token = self.arm()[3:]
        self.assertEqual(store.take_pending_note(7), PAGE)
        self.assertFalse(store.cancel_pending_note(7, token))
        self.assertIsNone(store.take_pending_note(7))

    def test_without_cancel_next_note_is_still_related(self):
        self.arm()
        pipeline.dispatch(self.message('这份资料的读后感', mid=2), False)
        self.assertEqual(self.create.call_args.kwargs['related'], [PAGE])
        self.assertEqual(self.create.call_args.kwargs['note_type'], '阅读笔记')
        self.assertIsNone(store.take_pending_note(7))

    def test_legacy_pending_without_token_is_still_consumed(self):
        self.db.collection('pending').document('7').set({'notion_page_id': PAGE, 'ts': 100})
        with patch.object(store.time, 'time', return_value=101):
            self.assertEqual(store.take_pending_note(7), PAGE)
        self.assertIsNone(store.take_pending_note(7))

    def test_expired_prompt_does_not_leave_pending_state(self):
        token = self.arm()[3:]
        self.db.data['pending']['7']['ts'] = 1
        self.assertFalse(store.cancel_pending_note(7, token))
        self.assertNotIn('7', self.db.data['pending'])

    def test_invalid_button_does_not_consume_pending_state(self):
        self.arm()
        pipeline.handle_callback(self.callback('nc:not-a-token'))
        self.assertEqual(store.take_pending_note(7), PAGE)
        self.edit.assert_not_called()

    def test_failed_prompt_does_not_leave_an_invisible_mode(self):
        self.send.side_effect = RuntimeError('telegram unavailable')
        with self.assertRaisesRegex(RuntimeError, 'telegram unavailable'):
            pipeline.dispatch(self.message(), False)
        self.assertIsNone(store.take_pending_note(7))


if __name__ == '__main__':
    unittest.main()
