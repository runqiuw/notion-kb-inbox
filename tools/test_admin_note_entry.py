"""Administrative imports must resolve the bot identity without retaining its token."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, telegram
from import_chat import configure_note_entry


def service(*env):
    return {'spec': {'template': {'spec': {'containers': [{'env': list(env)}]}}}}


class AdminEntryTests(unittest.TestCase):
    def setUp(self):
        for obj, name, value in [(config, 'TELEGRAM_BOT_TOKEN', ''),
                                  (config, 'TELEGRAM_BOT_USERNAME', ''),
                                  (telegram, '_me_username', '')]:
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_explicit_runtime_name_needs_no_secret_or_telegram_request(self):
        gc = Mock()
        with patch.object(telegram, '_call') as call:
            configure_note_entry(service({'name': 'TELEGRAM_BOT_USERNAME', 'value': 'actual_bot'}), gc)
            call.assert_not_called()
        gc.assert_not_called()
        self.assertEqual(telegram.note_deeplink('abc-def'), 'https://t.me/actual_bot?start=nabcdef')
        self.assertEqual(config.TELEGRAM_BOT_TOKEN, '')

    def test_secret_identity_lookup_clears_old_empty_cache_and_restores_token(self):
        gc = Mock(return_value='test-token\n')
        def get_me(method):
            self.assertEqual(method, 'getMe')
            self.assertEqual(config.TELEGRAM_BOT_TOKEN, 'test-token')
            return {'username': 'actual_bot'}
        with patch.object(telegram, '_call', side_effect=get_me) as call:
            configure_note_entry(service({'name': 'TELEGRAM_BOT_TOKEN', 'valueFrom': {
                'secretKeyRef': {'name': 'runtime-token', 'key': '7'}}}), gc)
        gc.assert_called_once_with('secrets', 'versions', 'access', '7', '--secret=runtime-token')
        call.assert_called_once_with('getMe')
        self.assertEqual(telegram.note_deeplink('abc-def'), 'https://t.me/actual_bot?start=nabcdef')
        self.assertEqual(config.TELEGRAM_BOT_TOKEN, '')

    def test_literal_token_is_only_used_for_read_only_identity_lookup(self):
        gc = Mock()
        with patch.object(telegram, '_call', return_value={'username': 'actual_bot'}) as call:
            configure_note_entry(service({'name': 'TELEGRAM_BOT_TOKEN', 'value': 'test-token'}), gc)
        call.assert_called_once_with('getMe')
        gc.assert_not_called()
        self.assertEqual(config.TELEGRAM_BOT_TOKEN, '')

    def test_unavailable_identity_fails_before_any_import(self):
        with patch.object(telegram, '_call') as call:
            with self.assertRaises(RuntimeError):
                configure_note_entry(service(), Mock())
            call.assert_not_called()

    def test_failed_get_me_does_not_retain_token_or_silently_omit_entry(self):
        for response in [{}, {'username': ''}]:
            with self.subTest(response=response), patch.object(telegram, '_call', return_value=response):
                with self.assertRaises(RuntimeError):
                    configure_note_entry(service({'name': 'TELEGRAM_BOT_TOKEN', 'value': 'test-token'}), Mock())
                self.assertEqual(config.TELEGRAM_BOT_TOKEN, '')


if __name__ == '__main__':
    unittest.main()
