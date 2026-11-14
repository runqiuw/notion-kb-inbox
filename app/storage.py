"""Original-artifact archive in GCS (plan section 15).

Notion holds the readable version; this holds the bytes, so the material
survives the source platform deleting it -- and survives Notion too.
"""
import datetime
import logging
import posixpath

from google.cloud import storage

from . import config

log = logging.getLogger(__name__)
_client: storage.Client | None = None


def _bucket():
    global _client
    if not config.GCS_BUCKET:
        return None
    if _client is None:
        _client = storage.Client()
    return _client.bucket(config.GCS_BUCKET)


def archive(data: bytes, name: str, content_type: str = "application/octet-stream") -> str:
    """Store bytes under a dated prefix. Returns gs:// URI, or "" if archiving
    is not configured or failed -- never raises into the ingestion path."""
    b = _bucket()
    if b is None:
        return ""
    day = datetime.datetime.utcnow().strftime("%Y/%m/%d")
    path = posixpath.join("originals", day, name)
    try:
        blob = b.blob(path)
        blob.upload_from_string(data, content_type=content_type)
        return f"gs://{config.GCS_BUCKET}/{path}"
    except Exception as e:
        log.warning("archive failed for %s: %s", path, e)
        return ""


def archive_text(text: str, name: str) -> str:
    return archive(text.encode("utf-8"), name, "text/plain; charset=utf-8")


def read_archive(uri: str, limit: int = 8 * 1024 * 1024) -> bytes:
    """Read only a bounded artifact from this service's own originals bucket."""
    prefix = 'gs://' + config.GCS_BUCKET + '/'
    if not config.GCS_BUCKET or not uri.startswith(prefix):
        raise ValueError('归档定位不属于当前原件桶')
    blob = _bucket().blob(uri[len(prefix):])
    blob.reload()
    if blob.size is None or blob.size > limit:
        raise ValueError('归档超过读取大小上限')
    return blob.download_as_bytes()
