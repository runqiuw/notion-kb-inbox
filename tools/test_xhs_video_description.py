"""Video media stays out of image OCR; XHS can save explicitly scoped descriptions."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parsehub import ParseHub
from parsehub.types.media_ref import VideoRef, ImageRef, LivePhotoRef
from parsehub.types.result import VideoParseResult, ImageParseResult, MultimediaParseResult
from app import enrich, extract, notion, pipeline, storage, store
import staging_stub

URL = 'https://xhslink.cn/o/video'
TITLE = '机器人测试'
DESCRIPTION = '发布者介绍了机器人任务测试框架，支持多种动作接口、仿真任务和真实机器人实验，并说明测试模式及其限制。'
MSG = {'chat': {'id': 1}, 'message_id': 311}


class VideoDescription(unittest.TestCase):
    def setUp(self):
        self.staged = staging_stub.install(store)
        for target, name, value in [
            (store, 'find_by_url', None), (store, 'pending_url_owner', None),
            (store, 'index_url', None), (storage, 'archive_text', 'gs://description'),
            (notion, 'append_blocks', None), (pipeline.thumbnails, 'material_art', {}),
        ]:
            p = patch.object(target, name, return_value=value)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(pipeline.config, 'GCP_PROJECT', '')
        p.start(); self.addCleanup(p.stop)
        self.art = {'ok': True, 'title': TITLE, 'text': DESCRIPTION, 'media_type': 'video',
                    'videos': ['https://cdn/movie.mp4'], 'images': [], 'thumbnail': 'https://cdn/cover.jpg'}
        self.summary = enrich.Enrichment(title='模型改的标题', overview='根据发布者简介，这是一套机器人测试框架。', key_points=['作者介绍了测试模式。'])

    def parse(self, result):
        with patch.object(ParseHub, 'parse_sync', return_value=result):
            return extract.parse_via_parsehub(URL)

    def test_typed_video_and_mixed_media_never_become_images(self):
        video = VideoRef(url='https://cdn/movie.mp4', thumb_url='https://cdn/cover.jpg')
        for media in (video, [video, ImageRef(url='https://cdn/1.jpg')]):
            result = (VideoParseResult(title=TITLE, content=DESCRIPTION, video=media)
                      if isinstance(media, VideoRef) else MultimediaParseResult(title=TITLE, content=DESCRIPTION, media=media))
            art = self.parse(result)
            self.assertEqual(art['media_type'], 'video')
            self.assertEqual(art['videos'], [video.url])
            self.assertNotIn(video.url, art['images'])
            self.assertNotIn(video.url, art['art_candidates']['body'])
            self.assertEqual(art['thumbnail'], video.thumb_url)

    def test_images_and_live_photo_stills_keep_existing_route(self):
        result = ImageParseResult(title=TITLE, photo=[ImageRef(url='https://cdn/1.jpg'),
            LivePhotoRef(url='https://cdn/still.jpg', video_url='https://cdn/live.mp4')])
        art = self.parse(result)
        self.assertEqual(art['images'], ['https://cdn/1.jpg', 'https://cdn/still.jpg'])
        self.assertEqual(art['videos'], [])
        self.assertFalse(pipeline._is_xhs_video('小红书', art))

    def test_fetch_preserves_type_through_platform_fallback(self):
        with patch.object(extract, 'parsehub_platform', return_value='XhsParser'), \
             patch.object(extract, 'parse_via_parsehub', return_value=self.art):
            art = pipeline.fetch_for(URL)
        self.assertEqual(art['media_type'], 'video')
        self.assertEqual(art['videos'], self.art['videos'])

    def test_staged_retry_saves_description_scope_title_cover_without_media_read(self):
        store.stage_url(URL, 1, 311, '原图过大')
        # Even old/stale image candidates must not leak into OCR or decoration.
        art = {**self.art, 'images': self.art['videos'] * 5, 'art_candidates': {'body': self.art['videos']}}
        with patch.object(pipeline, 'fetch_for', return_value=art), \
             patch.object(pipeline, '_read_url_images') as read, \
             patch.object(enrich, 'enrich', return_value=(self.summary, '')) as model, \
             patch.object(pipeline.thumbnails, 'material_art', return_value={}) as visuals, \
             patch.object(notion, 'create_material', return_value={'id': 'p', 'url': 'https://notion/p'}) as create, \
             patch.object(pipeline, '_save_link_thought') as thought:
            ok, _ = pipeline._retry_staged(store.get_staged(URL), lambda _: None)
        self.assertTrue(ok)
        self.assertFalse(store.get_staged(URL))
        read.assert_not_called(); thought.assert_not_called()
        self.assertEqual(model.call_args.args[1], DESCRIPTION)
        self.assertTrue(model.call_args.args[2].startswith(enrich.WEB_SUMMARY_HINT))
        self.assertIn('未解析视频画面或音轨', model.call_args.args[2])
        self.assertEqual(model.call_args.kwargs['images'], [])
        kw = create.call_args.kwargs
        self.assertEqual((kw['title'], kw['material_type'], kw['processing']), (TITLE, '视频', '完成'))
        self.assertEqual(kw['url'], URL)
        self.assertEqual(visuals.call_args.kwargs['candidates'], {'shares': [art['thumbnail']], 'body': []})
        body = str(kw['blocks'])
        self.assertIn(DESCRIPTION, body)
        self.assertIn(pipeline.VIDEO_DESCRIPTION_SCOPE, body)
        self.assertIn('发布者简介原文', body)
        self.assertNotIn('逐图总结', body)
        self.assertNotIn('movie.mp4', body)

    def test_title_and_hashtags_only_stay_staged_without_model_call(self):
        for description in ('', TITLE, TITLE + '\n#机器人 #具身智能[话题]#', '#verylonghashtag' * 30):
            with self.subTest(description=description), \
                 patch.object(pipeline, 'fetch_for', return_value={**self.art, 'text': description}), \
                 patch.object(enrich, 'enrich') as model, \
                 patch.object(notion, 'create_material') as create:
                pipeline._handle_url(MSG, URL, '这是用户附带的长长评论，不得作为简介补充。' * 5)
                self.assertTrue(store.get_staged(URL))
                model.assert_not_called(); create.assert_not_called()

    def test_summary_failure_stays_staged(self):
        with patch.object(pipeline, 'fetch_for', return_value=self.art), \
             patch.object(enrich, 'enrich', return_value=(self.summary, '模型失败')), \
             patch.object(notion, 'create_material') as create:
            pipeline._handle_url(MSG, URL, '')
        create.assert_not_called()
        self.assertIn('模型失败', store.get_staged(URL)['error'])

    def test_existing_pending_page_retry_uses_same_scope(self):
        page = {'id': 'p', 'properties': {'标题': {'type': 'title', 'title': [{'plain_text': TITLE}]},
                '原始链接': {'type': 'url', 'url': URL}}}
        with patch.object(pipeline, 'fetch_for', return_value=self.art), \
             patch.object(pipeline, '_read_url_images') as read, \
             patch.object(enrich, 'enrich', return_value=(self.summary, '')), \
             patch.object(notion, 'append_blocks') as append, \
             patch.object(notion, 'refresh_machine_fields') as refresh:
            ok, _ = pipeline._retry_one(page, lambda _: None)
        self.assertTrue(ok); read.assert_not_called()
        self.assertIn(pipeline.VIDEO_DESCRIPTION_SCOPE, str(append.call_args.args))
        self.assertEqual(refresh.call_args.kwargs['material_type'], '视频')

    def test_description_exception_is_limited_to_xhs(self):
        self.assertEqual(pipeline.VIDEO_PLATFORMS, {'YouTube', 'B站'})
        self.assertFalse(pipeline._is_xhs_video('B站', self.art))
        self.assertFalse(pipeline._is_xhs_video('YouTube', self.art))


if __name__ == '__main__':
    unittest.main()
