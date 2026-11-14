"""Markdown sources must resolve to their destination without damaging notes."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import extract, pipeline

URL = 'https://cupid-curation.github.io/'


class UrlExtractionTests(unittest.TestCase):
    def test_production_markdown_message_routes_url_without_note(self):
        text = f'我有一个想法，比如[{URL}]({URL})。机器人应该能从死胡同走出来。'
        msg = {'text': text, 'message_id': 249, 'chat': {'id': 7}}
        with patch.object(pipeline, '_handle_url', return_value=('saved', 'page')) as handle:
            self.assertEqual(pipeline.first_url(msg), URL)
            pipeline.dispatch(msg, False)
        self.assertEqual(handle.call_args.args[1], URL)
        self.assertEqual(handle.call_args.args[2], '')
        self.assertEqual(extract.find_urls(text), [URL])

    def test_markdown_destination_wins_over_url_label(self):
        self.assertEqual(extract.find_urls(f'[https://old.example/]({URL})'), [URL])

    def test_named_label_and_other_urls_remain_in_note(self):
        text = f'参考[CUPID 项目]({URL})，再对照 https://example.org/other。'
        self.assertEqual(extract.find_urls(text), [URL, 'https://example.org/other'])
        self.assertEqual(extract.without_first_url(text),
                         '参考CUPID 项目，再对照 https://example.org/other。')

    def test_balanced_parentheses_and_ipv6_are_preserved(self):
        for url in ('https://example.org/A_(B_(C))', 'http://[::1]:8080/path',
                    'https://doi.org/10.1016/S0000-0000(01)00100-0'):
            with self.subTest(url=url):
                self.assertEqual(extract.find_urls(f'({url})。'), [url])
                self.assertEqual(extract.find_urls(f'[来源]({url})'), [url])

    def test_angle_destination_and_optional_titles(self):
        for suffix in ('', ' "论文标题"', " '论文标题'", ' (论文标题)'):
            with self.subTest(suffix=suffix):
                text = f'我的想法 [项目](<{URL}>{suffix}) 后面还有一句。'
                self.assertEqual(extract.find_urls(text), [URL])
                self.assertEqual(extract.without_first_url(text), '我的想法 项目 后面还有一句。')

    def test_escaped_parentheses_in_markdown_destination(self):
        self.assertEqual(extract.find_urls(r'[来源](https://example.org/A_\(B\))'),
                         ['https://example.org/A_(B)'])

    def test_plain_links_keep_previous_punctuation_behavior(self):
        self.assertEqual(extract.find_urls('看 https://example.org/a。 再看 https://example.org/b!'),
                         ['https://example.org/a', 'https://example.org/b'])
        self.assertEqual(extract.find_urls('[https://example.org/a]'), ['https://example.org/a'])
        self.assertEqual(extract.find_urls('没有网址'), [])
        self.assertEqual(extract.without_first_url('普通想法'), '普通想法')

    def test_first_source_order_and_only_first_occurrence_removed(self):
        text = f'https://example.org/first [项目]({URL})'
        self.assertEqual(extract.find_urls(text), ['https://example.org/first', URL])
        self.assertEqual(extract.without_first_url(text), f' [项目]({URL})')
        self.assertEqual(extract.without_first_url(f'[{URL}]({URL}) 然后 {URL}'), f' 然后 {URL}')


if __name__ == '__main__':
    unittest.main()
