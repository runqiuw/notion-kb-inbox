"""Only an explicit reply can relate independently captured text and sources."""
import unittest
from unittest.mock import patch
from test_link_thoughts import LinkThoughtTests, URL, NOTE
from app import enrich, notion, pipeline, store


class ReplyPolicyTests(unittest.TestCase):
    setUp = LinkThoughtTests.setUp
    patch = LinkThoughtTests.patch
    msg = LinkThoughtTests.msg
    pages = LinkThoughtTests.pages

    def relation(self, page):
        return [r['id'] for r in page['properties'].get('关联资料', {}).get('relation', [])]

    def test_blog_share_only_extracts_source_without_creating_note(self):
        pipeline.dispatch(self.msg(note='', url='可以看下我们的第一个blog，比较详细：' + URL), False)
        self.assertEqual(len(self.pages('materials')), 1)
        self.assertEqual(self.pages('thoughts'), [])

    def test_markdown_named_source_only_is_not_a_personal_note(self):
        pipeline.dispatch(self.msg(note='', url=f'[项目主页]({URL})'), False)
        self.assertEqual(len(self.pages('materials')), 1)
        self.assertEqual(self.pages('thoughts'), [])

    def test_attached_private_prose_is_ignored_and_never_enters_source_summary(self):
        raw = f'723 project\n我的想法：比如[{URL}]({URL})。机器人需要能纠错。\n771 project'
        with patch.object(enrich, 'enrich', return_value=(enrich.Enrichment(title='资料', overview='概览'), '')) as summarize:
            pipeline.dispatch({**self.msg(), 'text': raw}, False)
        self.assertEqual(summarize.call_args.kwargs.get('note', ''), '')
        self.assertEqual(self.pages('thoughts'), [])
        self.assertNotIn('link_thought', store.get_record(7, 1))
        self.assertEqual(self.n.mirrors(self.pages('materials')[0]['id']), [])

    def test_text_replying_to_source_links_only_quoted_material(self):
        pipeline.dispatch(self.msg(note=''), False)
        material = self.pages('materials')[0]
        pipeline.dispatch({**self.msg(mid=2), 'text': NOTE, 'reply_to_message': {'message_id': 1}}, False)
        note = self.pages('thoughts')[0]
        self.assertEqual(self.relation(note), [material['id']])
        self.assertEqual(len(self.n.mirrors(material['id'])), 1)

    def test_link_in_long_message_ignores_all_attached_text(self):
        raw = f'我的想法\n\n[项目]({URL})\n    另一个想法'
        pipeline.dispatch({**self.msg(), 'text': raw}, False)
        self.assertEqual(self.pages('thoughts'), [])
        self.assertEqual(store.get_record(7, 1)['url'], URL)

    def test_source_replying_to_text_links_existing_note(self):
        pipeline.dispatch({**self.msg(), 'text': NOTE}, False)
        note = self.pages('thoughts')[0]
        pipeline.dispatch({**self.msg(mid=2, note=''), 'reply_to_message': {'message_id': 1}}, False)
        material = self.pages('materials')[0]
        self.assertEqual(self.relation(note), [material['id']])
        self.assertEqual(len(self.pages('thoughts')), 1)
        self.assertEqual(len(self.n.mirrors(material['id'])), 1)

    def test_source_reply_keeps_existing_relations_and_dedupes_material(self):
        pipeline.dispatch({**self.msg(), 'text': NOTE}, False)
        for mid, url in [(2, URL), (3, URL + '/other'), (4, URL)]:
            pipeline.dispatch({**self.msg(mid=mid, note='', url=url), 'reply_to_message': {'message_id': 1}}, False)
        self.assertEqual(len(self.pages('materials')), 2)
        self.assertEqual(len(self.relation(self.pages('thoughts')[0])), 2)
        for material in self.pages('materials'):
            self.assertEqual(len(self.n.mirrors(material['id'])), 1)

    def test_url_reply_does_not_turn_attached_text_into_a_note(self):
        pipeline.dispatch(self.msg(note=''), False)
        pipeline.dispatch({**self.msg(mid=2, url=URL + '/other'), 'reply_to_message': {'message_id': 1}}, False)
        self.assertEqual(self.pages('thoughts'), [])
        self.assertEqual(len(self.pages('materials')), 2)

    def test_text_reply_before_material_finishes_survives_retry(self):
        self.fetch.side_effect = lambda *a: {'ok': False, 'title': '', 'text': '', 'error': 'offline'}
        pipeline.dispatch(self.msg(note=''), False)
        pipeline.dispatch({**self.msg(mid=2), 'text': NOTE, 'reply_to_message': {'message_id': 1}}, False)
        self.fetch.side_effect = lambda *a: {'ok': True, 'title': '资料', 'text': '正文', 'error': ''}
        pipeline._handle_retry(lambda _: None)
        self.assertEqual(self.relation(self.pages('thoughts')[0]), [self.pages('materials')[0]['id']])

    def test_source_reply_survives_failed_fetch_and_reconstructed_retry_message(self):
        pipeline.dispatch({**self.msg(), 'text': NOTE}, False)
        self.fetch.side_effect = lambda *a: {'ok': False, 'title': '', 'text': '', 'error': 'offline'}
        pipeline.dispatch({**self.msg(mid=2, note=''), 'reply_to_message': {'message_id': 1}}, False)
        self.fetch.side_effect = lambda *a: {'ok': True, 'title': '资料', 'text': '正文', 'error': ''}
        pipeline._handle_retry(lambda _: None)
        self.assertEqual(self.relation(self.pages('thoughts')[0]), [self.pages('materials')[0]['id']])

    def test_source_reply_before_original_text_task_runs(self):
        pipeline.dispatch({**self.msg(mid=2, note=''), 'reply_to_message': {'message_id': 1}}, False)
        pipeline.dispatch({**self.msg(), 'text': NOTE}, False)
        self.assertEqual(self.relation(self.pages('thoughts')[0]), [self.pages('materials')[0]['id']])

    def test_legacy_automatic_stage_ids_do_not_authorize_a_relation(self):
        note = notion.create_thought(title=NOTE, note_type='随想', topics=[], related=[], blocks=notion.paragraphs(NOTE))
        store.stage_url(URL, 7, 2, 'offline', [note['id']])
        pipeline._handle_retry(lambda _: None)
        self.assertEqual(self.relation(note), [])
        self.assertEqual(self.n.mirrors(self.pages('materials')[0]['id']), [])


if __name__ == '__main__':
    unittest.main()
