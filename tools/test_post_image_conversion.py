"""Actual HEIC conversion, original archival, and completeness on ingest/retry."""
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import pillow_heif
from PIL import Image
from app import enrich, extract, pipeline, storage, store, notion
import staging_stub


class PostImages(unittest.TestCase):
    def setUp(self):
        mock = patch.object(pipeline.comments, "capture", return_value=None)
        mock.start(); self.addCleanup(mock.stop)

    @classmethod
    def setUpClass(cls):
        pillow_heif.register_heif_opener()
        image = Image.new('RGB', (800, 1800), 'white')
        buf = io.BytesIO()
        image.save(buf, format='HEIF')
        cls.heic = buf.getvalue()

    def response(self, data, status=200):
        return httpx.Response(status, content=data, headers={'content-type': 'image/jpeg'},
                              request=httpx.Request('GET', 'https://cdn/image'))

    def test_heic_mislabelled_as_jpeg_is_converted_and_original_is_unchanged(self):
        with patch.object(extract.httpx, 'get', return_value=self.response(self.heic)):
            images, missing = extract.download_images(['https://cdn/1'])
        self.assertEqual(missing, [])
        self.assertEqual(images.originals, [(0, self.heic, 'image/heic')])
        self.assertEqual(len(images), 1)
        data, mime = images[0]
        self.assertEqual(extract.sniff_image_type(data), mime)
        self.assertEqual(mime, 'image/png')
        with Image.open(io.BytesIO(data)) as decoded:
            self.assertEqual(decoded.size, (800, 1800))  # Not a 1400px thumbnail.
        with patch.object(extract, 'download_images', return_value=(images, [])), \
             patch.object(storage, 'archive', return_value='gs://original') as archive:
            vision, stored, failed = pipeline._read_url_images({'images': ['https://cdn/1']}, 'https://xhslink.cn/a', lambda _: None)
        self.assertIs(vision, images)
        self.assertEqual(stored, ['gs://original'])
        self.assertFalse(failed)
        self.assertEqual(archive.call_args.args[0], self.heic)
        self.assertTrue(archive.call_args.args[1].endswith('-0.heic'))
        self.assertEqual(archive.call_args.args[2], 'image/heic')

    def test_bad_images_are_accounted_for_without_losing_good_originals(self):
        urls = ['https://cdn/' + str(i) for i in range(5)]
        results = [self.response(self.heic), self.response(b'<html>error</html>'),
                   self.response(b'bad', 403), self.response(b'x' * (extract.MAX_IMAGE_BYTES + 1)),
                   httpx.ConnectError('offline')]
        with patch.object(extract.httpx, 'get', side_effect=results):
            images, missing = extract.download_images(urls)
        self.assertEqual(len(images), 1)
        self.assertEqual(missing, urls[1:])
        self.assertEqual([x[0] for x in images.originals], [0, 1])

    def test_count_and_both_byte_budgets_report_missing_images(self):
        urls = ['https://cdn/' + str(i) for i in range(11)]
        with patch.object(extract.httpx, 'get', return_value=self.response(self.heic)) as get:
            images, missing = extract.download_images(urls)
        self.assertEqual(get.call_count, extract.MAX_IMAGES)
        self.assertEqual(missing, urls[extract.MAX_IMAGES:])
        for converted in (False, True):
            with self.subTest(converted=converted), \
                 patch.object(extract.httpx, 'get', return_value=self.response(self.heic)), \
                 patch.object(extract, 'MAX_TOTAL_IMAGE_BYTES', len(self.heic) + 1), \
                 patch.object(extract, 'vision_image', return_value=(b'x' * (len(self.heic) + 2) if converted else b'x', 'image/png')):
                images, missing = extract.download_images(urls[:2])
            self.assertEqual(len(missing), 2 if converted else 1)
            self.assertLessEqual(sum(len(x[0]) for x in images), len(self.heic) + 1)

    def test_pixel_cap_is_checked_before_decoding(self):
        opened = Mock(width=6000, height=5000)
        context = Mock()
        context.__enter__ = Mock(return_value=opened)
        context.__exit__ = Mock(return_value=False)
        with patch.object(Image, 'open', return_value=context):
            with self.assertRaisesRegex(ValueError, 'too many pixels'):
                extract.vision_image(self.heic)
        opened.convert.assert_not_called()

    def test_all_missing_stages_before_model_and_never_creates_completed_page(self):
        art = {'ok': True, 'title': '帖子', 'text': '引言', 'images': ['https://cdn/1']}
        with patch.multiple(store, find_by_url=lambda _: None, pending_url_owner=lambda _: None):
            staged = staging_stub.install(store)
            with patch.object(pipeline, 'fetch_for', return_value=art), \
                 patch.object(extract, 'download_images', return_value=([], art['images'])), \
                 patch.object(storage, 'archive_text', return_value='gs://body'), \
                 patch.object(enrich, 'enrich') as model, \
                 patch.object(pipeline, '_create_material') as create:
                pipeline._handle_url({'chat': {'id': 1}, 'message_id': 1}, 'https://xhslink.cn/a', '')
            self.assertTrue(staged)
        model.assert_not_called()
        create.assert_not_called()

    def test_retry_reads_images_and_cannot_mark_a_missing_batch_complete(self):
        page = {'id': 'p', 'properties': {'标题': {'type': 'title', 'title': [{'plain_text': '帖子'}]},
                '原始链接': {'type': 'url', 'url': 'https://xhslink.cn/a'}}}
        art = {'ok': True, 'title': '帖子', 'text': '引言', 'images': ['https://cdn/1']}
        summary = enrich.Enrichment(title='帖子', overview='真实图片内容', key_points=['图片要点'],
                    images=[enrich.ImageRead(index=0, informative=True, summary='图片总结', text='原始图片文字')])
        with patch.object(pipeline, 'fetch_for', return_value=art), \
             patch.object(extract, 'download_images', return_value=([(b'png', 'image/png')], [])), \
             patch.object(storage, 'archive', return_value='gs://image'), \
             patch.object(enrich, 'enrich', return_value=(summary, '')) as model, \
             patch.object(notion, 'append_blocks') as append, \
             patch.object(notion, 'refresh_machine_fields') as refresh:
            ok, _ = pipeline._retry_one(page, lambda _: None)
            self.assertTrue(ok)
            self.assertEqual(model.call_args.kwargs['images'], [(b'png', 'image/png')])
            self.assertIn('原始图片文字', str(append.call_args.args[1]))
            self.assertEqual(refresh.call_args.kwargs['processing'], '完成')
            append.reset_mock(); refresh.reset_mock(); model.reset_mock()
            with patch.object(extract, 'download_images', return_value=([], art['images'])):
                ok, _ = pipeline._retry_one(page, lambda _: None)
            self.assertFalse(ok)
            model.assert_not_called(); append.assert_not_called(); refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
