"""Cover extraction and failure isolation; no network or Notion writes."""
import os
import io
import sys
from PIL import Image
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import thumbnails, notion, extract

assert thumbnails.youtube_thumbnail("https://youtu.be/abcdefghijk?t=5").endswith("/abcdefghijk/hqdefault.jpg")
assert thumbnails.youtube_thumbnail("https://www.youtube.com/shorts/abcdefghijk").endswith("/abcdefghijk/hqdefault.jpg")
assert not thumbnails.youtube_thumbnail("https://youtube.com.evil.test/watch?v=abcdefghijk")
assert thumbnails.html_thumbnail('<meta content="/cover.jpg?a=1&amp;b=2" property="og:image">', 'https://example.com/post') == 'https://example.com/cover.jpg?a=1&b=2'
assert not thumbnails.html_thumbnail('<meta property="og:image" content="javascript:alert(1)">', 'https://example.com')

class Response:
    def __init__(self, blob): self.blob = blob; self.headers = {}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def raise_for_status(self): pass
    def iter_bytes(self): yield self.blob

blob = io.BytesIO(); Image.new('RGB', (300, 200), 'blue').save(blob, 'JPEG')
with patch('httpx.stream', return_value=Response(blob.getvalue())), patch.object(notion, 'upload_file', return_value='uploaded'):
    assert thumbnails.upload_thumbnail('https://example.com/cover.jpg')['file_upload']['id'] == 'uploaded'
for invalid in (b'<html>blocked</html>', b'x' * (extract.MAX_IMAGE_BYTES + 1)):
    thumbnails._downloads.clear()
    with patch('httpx.stream', return_value=Response(invalid)), patch.object(notion, 'upload_file') as upload:
        assert thumbnails.upload_thumbnail('https://example.com/cover.jpg') is None
        upload.assert_not_called()
with patch('httpx.stream', side_effect=RuntimeError('offline')):
    assert thumbnails.upload_thumbnail('https://example.com/cover.jpg') is None
print('PASS')
