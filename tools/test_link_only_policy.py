"""Real share-message regressions for the temporary link-only policy."""
import copy
import unittest
from unittest.mock import patch
import test_link_thoughts as fixtures
import test_video as video_fixtures
from app import enrich, pipeline, store, video

XHS = '斯坦福新作：砍掉腕部相机，成功率反而更高 斯坦福 ×... https://xhslink.cn/o/1DfnGF3ajUF 拷走口令，来【小红书】瞅瞅~'
BILI = '【李飞飞收购郑昌熙：有生之年，我们可能看不到通用机器人出现。-哔哩哔哩】 https://b23.tv/6yG6ICk'


class LinkOnly(unittest.TestCase):
    setUp = fixtures.LinkThoughtTests.setUp
    patch = fixtures.LinkThoughtTests.patch
    pages = fixtures.LinkThoughtTests.pages

    def test_real_xhs_share_and_genuine_comment_only_save_source(self):
        for mid, text in enumerate((XHS, fixtures.URL + ' 我自己的新观点，绝不能归给来源'), 1):
            with self.subTest(text=text):
                before = copy.deepcopy(self.n.pages)
                with patch.object(enrich, 'enrich', return_value=(enrich.Enrichment(title='资料', overview='概览'), '')) as model:
                    pipeline.dispatch({'chat': {'id': 7}, 'message_id': mid, 'text': text}, False)
                self.assertEqual(model.call_args.kwargs['note'], '')
                self.assertNotIn('我自己的新观点', str(model.call_args))
                self.assertNotIn('拷走口令', str(model.call_args))
                self.assertEqual(self.pages('thoughts'), [])
                self.assertEqual(len(self.pages('materials')), mid)
                for pid, page in before.items():
                    self.assertEqual(self.n.pages[pid], page)

    def test_real_bili_share_routes_only_the_extracted_url(self):
        with patch.object(pipeline, '_handle_video', return_value=('queued', '')) as capture:
            pipeline.dispatch({'chat': {'id': 7}, 'message_id': 1, 'text': BILI}, False)
        args = capture.call_args.args
        self.assertEqual(args[1:3], ('https://b23.tv/6yG6ICk', ''))
        self.assertEqual(self.pages('thoughts'), [])

    def test_url_captions_are_ignored_without_mutating_raw_message(self):
        for key, handler in [('photo', '_handle_photo'), ('document', '_handle_document')]:
            with self.subTest(kind=key):
                raw = {'chat': {'id': 7}, 'message_id': 1, key: {'file_id': 'f'}, 'caption': XHS}
                before = copy.deepcopy(raw)
                with patch.object(pipeline, handler, return_value=('saved', '')) as capture:
                    pipeline.dispatch(raw, False)
                self.assertEqual(capture.call_args.args[0]['caption'], '')
                self.assertEqual(raw, before)


class LegacyVideo(unittest.TestCase):
    setUp = video_fixtures.Videos.setUp
    p = video_fixtures.Videos.p
    archive = video_fixtures.Videos.archive
    bili = video_fixtures.Videos.bili
    youtube = video_fixtures.Videos.youtube
    gemini = video_fixtures.Videos.gemini
    model = video_fixtures.Videos.model

    def test_old_waiting_job_does_not_resurrect_attached_text(self):
        self.dm = {'subtitles': None}
        url = f'https://www.bilibili.com/video/{video_fixtures.BV}?p=2'
        pipeline._handle_url(self.msg, url, '旧的分享标题', lambda _: None, 1)
        key = f'bilibili:{video_fixtures.BV}:222'
        store.update_video_job(key, note='旧的分享标题', next_retry_at=0)
        self.dm = {'subtitles': [{'lan': 'ai-zh', 'type': 1, 'subtitle_url': '//aisubtitle.hdslb.com/bfs/sub.json?auth_key=signed'}]}
        with patch.object(enrich, 'enrich', wraps=enrich.enrich) as model:
            video.resume(key)
        self.assertEqual(store.get_video_job(key)['status'], 'done')
        for call in model.call_args_list:
            self.assertEqual(call.kwargs.get('note', ''), '')
        self.assertFalse([p for p in self.n.pages.values() if p['parent'].get('database_id') == 'thoughts'])


if __name__ == '__main__':
    unittest.main()
