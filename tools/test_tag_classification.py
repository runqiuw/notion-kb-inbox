"""Source evidence, uncertainty and reply-only suggestions across ingestion paths."""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import chat_notes, chat_share, config, enrich, pipeline, tag_store, topics


def evidence(tag, quote='提出新的位置编码机制'):
    return {'tag': tag, 'basis': '主要研究该机制', 'quote': quote}


def answer(tags, proofs=None, review=None):
    return {'title': '测试', 'overview': '来源的概览', 'material_type': '论文', 'topics': tags,
            'topic_evidence': proofs or [],
            'tag_review': review or {'needed': False, 'reason': '', 'suggestions': []}}


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.vocabulary = tag_store.seed()
        self.provider = patch.object(enrich.settings, 'active', return_value={
            'base_url': 'https://example.test/v1', 'model': 'test', 'api_key': 'test'})
        self.provider.start()
        self.addCleanup(self.provider.stop)

    def audit(self, data, source='提出新的位置编码机制；发布新的评测基准。'):
        return topics.audit(data, source, self.vocabulary)

    def test_confirmed_algorithm_not_demoted(self):
        tags, proofs, review = self.audit(answer(['ml'], [evidence('ML')]))
        self.assertEqual(tags, ['ML'])
        self.assertEqual(proofs[0]['tag'], 'ML')
        self.assertFalse(review['needed'])

    def test_missing_and_fabricated_evidence_preserve_other_confirmed_tags(self):
        tags, proofs, review = self.audit(answer(['ML', 'MLSys', 'Benchmark'],
            [evidence('MLSys', '这个不存在的算子原文'), evidence('Benchmark', '发布新的评测基准')]))
        self.assertEqual(tags, ['Benchmark'])
        self.assertEqual([p['tag'] for p in proofs], ['Benchmark'])
        self.assertEqual(review['suggestions'], ['ML', 'MLSys'])
        self.assertIn('原文依据', review['reason'])

    def test_uncertain_tag_is_never_committed_even_with_source_quote(self):
        tags, _, review = self.audit(answer(['ML', 'Benchmark'],
            [evidence('ML'), evidence('Benchmark', '发布新的评测基准')],
            {'needed': True, 'reason': '算法机制与执行优化的贡献边界不清楚', 'suggestions': ['ML', 'MLSys']}))
        self.assertEqual(tags, ['Benchmark'])
        self.assertTrue(review['needed'])
        self.assertEqual(review['suggestions'], ['ML', 'MLSys'])

    def test_ml_llm_ambiguity_requests_main_contribution(self):
        tags, _, review = self.audit(answer(['ML', 'LLM'], [evidence('ML'), evidence('LLM')]))
        self.assertEqual(tags, [])
        self.assertEqual(review['suggestions'], ['ML', 'LLM'])
        self.assertIn('主要贡献', review['reason'])

    def test_no_matching_vocabulary_reports_gap_without_creating_tag(self):
        tags, _, review = self.audit(answer([], review={
            'needed': True, 'reason': '现有标签暂无合适项：主要讨论植物养护', 'suggestions': ['Gardening', 'GPT-6']}))
        self.assertEqual(tags, [])
        self.assertEqual(review['suggestions'], [])
        self.assertIn('暂无合适项', pipeline._tag_review_text(review))

    def test_disabled_tags_are_excluded(self):
        next(e for e in self.vocabulary['entries'] if e['name'] == 'ML')['enabled'] = False
        self.assertEqual(self.audit(answer(['ML'], [evidence('ML')]))[0], [])

    def test_original_quote_accepts_whitespace_but_not_generated_overview(self):
        self.assertEqual(self.audit(answer(['ML'], [evidence('ML')]), '提出新的\n位置编码机制')[0], ['ML'])
        data = answer(['ML'], [evidence('ML', '来源的概览')])
        self.assertEqual(self.audit(data)[0], [])

    def test_malformed_tag_fields_do_not_block_material(self):
        data = answer('ML', proofs='bad', review='bad')
        with patch.object(enrich, '_chat', return_value=json.dumps(data)), \
             patch.object(config, 'LLM_BASE_URL', 'https://example.test/v1'), \
             patch.object(tag_store, 'load', return_value=self.vocabulary):
            result, error = enrich.enrich('原标题', '提出新的位置编码机制')
        self.assertEqual(error, '')
        self.assertEqual(result.topics, [])
        self.assertTrue(result.tag_review['needed'])

    def test_material_validation_and_html_reply_keep_suggestions_separate(self):
        data = answer(['ML', 'MLSys'], [evidence('ML')], {
            'needed': True, 'reason': '未说明 <GPU> 执行瓶颈', 'suggestions': ['MLSys']})
        with patch.object(enrich, '_chat', return_value=json.dumps(data)), \
             patch.object(config, 'LLM_BASE_URL', 'https://example.test/v1'), \
             patch.object(tag_store, 'load', return_value=self.vocabulary):
            result, error = enrich.enrich('原标题', '提出新的位置编码机制')
        self.assertEqual(error, '')
        self.assertEqual(result.topics, ['ML'])
        text = pipeline._success_text('已整理', result.title, tag_review=result.tag_review)
        self.assertIn('&lt;GPU&gt;', text)
        self.assertIn('建议标签：MLSys', text)
        self.assertNotIn('tag_review', text)
        self.assertNotIn('标签待确认', pipeline._success_text('已整理', '标题'))

    def test_staged_retry_returns_review_for_success(self):
        with patch.object(pipeline.store, 'get_staged', return_value=None), \
             patch.object(pipeline.chat_share, 'is_share', return_value=False), \
             patch.object(pipeline, '_handle_url', return_value=('已整理\n标签待确认：依据不足\n建议标签：ML', 'page')):
            ok, detail = pipeline._retry_staged({'url': 'https://example.test/paper'}, lambda _: None)
        self.assertTrue(ok)
        self.assertIn('标签待确认', detail)

    def test_batch_retry_stays_within_telegram_limit_without_breaking_html(self):
        detail = pipeline._tag_review_text({'needed': True, 'reason': '<执行瓶颈>' * 60,
                                           'suggestions': ['ML', 'MLSys']})
        rows = [{'url': f'https://example.test/{i}'} for i in range(10)]
        with patch.object(pipeline.store, 'staged_urls', return_value=rows), \
             patch.object(pipeline.notion, 'find_materials', return_value=[]), \
             patch.object(pipeline, '_retry_staged', return_value=(True, detail)), \
             patch.object(pipeline.store, 'waiting_for_llm', return_value=[]), \
             patch.object(pipeline.work_budget, 'remaining', return_value=1000):
            text, _ = pipeline._retry_pending(lambda _: None)
        self.assertLess(len(text), 4096)
        self.assertIn('10 条成功', text)
        self.assertIn('未展开', text)
        self.assertIn('建议标签：ML / MLSys', text)
        self.assertNotIn('<执行瓶颈>', text)

    def test_chat_and_segment_fallback_keep_source_evidence_and_reply_review(self):
        c = chat_share.Conversation('ChatGPT', 'https://example.test', '讨论编码', [
            {'id': 1, 'role': 'user', 'text': '提出新的位置编码机制'},
            {'id': 2, 'role': 'assistant', 'text': '进一步比较具体实现'}])
        data = {**answer(['ML'], [evidence('ML')]),
                **{s: [{'text': '编码机制讨论', 'sources': [1]}] for s in chat_notes.SECTIONS}}
        data['ai_suggestions'][0]['sources'] = [2]
        with patch.object(tag_store, 'load', return_value=self.vocabulary):
            first = chat_notes.validate_digest(copy.deepcopy(data), c)
            second = copy.deepcopy(first)
            second['tag_review'] = {'needed': True, 'reason': '执行优化依据不足', 'suggestions': ['MLSys']}
            combined = chat_notes.combine_digests(c, [first, second])
        self.assertEqual(combined['topics'], ['ML'])
        self.assertEqual(combined['tag_review']['suggestions'], ['MLSys'])
        self.assertTrue(combined['tag_review']['needed'])


if __name__ == '__main__':
    unittest.main()
