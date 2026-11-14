"""Cloud Tasks hand-off.

The webhook must return fast and must not lose work, so it writes the update to
Firestore and enqueues a task. Cloud Tasks owns retry/backoff -- that is the
"可以重试" requirement without us writing a retry loop.
"""
import datetime
import json
import logging

from google.cloud import tasks_v2
from google.protobuf import duration_pb2, timestamp_pb2

from . import config

log = logging.getLogger(__name__)
_client: tasks_v2.CloudTasksClient | None = None


def client() -> tasks_v2.CloudTasksClient:
    global _client
    if _client is None:
        _client = tasks_v2.CloudTasksClient()
    return _client


def enqueue_process(update_id: int, redispatch: int = 0,
                    delay_seconds: int = 0) -> bool:
    """Returns True if the task was queued. False means the caller must fall
    back to processing inline rather than dropping the update.

    `redispatch` goes into the task name. Cloud Tasks keeps a finished task's
    name for about an hour and rejects a duplicate with ALREADY_EXISTS, so a
    drain that re-sent the original name would be refused, return False, and
    look like it had simply found nothing to do.

    `delay_seconds` holds the task back, which is how an album is collected:
    Telegram never says which photo is the last one, so the update that claimed
    the group waits a few seconds for its siblings to register instead of
    guessing.
    """
    if not (config.GCP_PROJECT and config.SERVICE_URL and config.TASKS_SA_EMAIL):
        return False
    global _client
    try:
        _client = client()
        parent = _client.queue_path(config.GCP_PROJECT, config.GCP_REGION,
                                    config.TASKS_QUEUE)
        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{config.SERVICE_URL.rstrip('/')}/tasks/process",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"update_id": update_id}).encode(),
                "oidc_token": {
                    "service_account_email": config.TASKS_SA_EMAIL,
                    "audience": config.SERVICE_URL.rstrip("/"),
                },
            },
            # Dedupe: Telegram retries webhooks, and we never want two pages.
            # The suffix scopes that dedupe to one dispatch round.
            "name": _client.task_path(
                config.GCP_PROJECT, config.GCP_REGION, config.TASKS_QUEUE,
                f"update-{update_id}" if not redispatch
                else f"update-{update_id}-r{redispatch}"),
        }
        if delay_seconds:
            when = timestamp_pb2.Timestamp()
            when.FromDatetime(datetime.datetime.now(datetime.timezone.utc)
                              + datetime.timedelta(seconds=delay_seconds))
            task["schedule_time"] = when
        _client.create_task(parent=parent, task=task)
        return True
    except Exception as e:
        log.warning("enqueue failed for %s: %s", update_id, e)
        return False


def enqueue_tags(job_id: str, round_id: str = '') -> bool:
    """Tag preview/sync shares the existing queue, with a separate worker route."""
    if not (config.GCP_PROJECT and config.SERVICE_URL and config.TASKS_SA_EMAIL):
        return False
    from google.api_core.exceptions import AlreadyExists
    try:
        c = client()
        c.create_task(parent=c.queue_path(config.GCP_PROJECT, config.GCP_REGION, config.TASKS_QUEUE), task={
            'name': c.task_path(config.GCP_PROJECT, config.GCP_REGION, config.TASKS_QUEUE,
                                f'tags-{job_id}-{round_id or "initial"}'),
            'http_request': {'http_method': tasks_v2.HttpMethod.POST,
                'url': config.SERVICE_URL.rstrip('/') + '/tasks/tags',
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'job_id': job_id}).encode(),
                'oidc_token': {'service_account_email': config.TASKS_SA_EMAIL,
                               'audience': config.SERVICE_URL.rstrip('/')}}})
        return True
    except AlreadyExists:
        return True
    except Exception as e:
        log.warning('tag enqueue failed: %s', e)
        return False


def enqueue_images(url: str, round_id: int, delay_seconds: int = 1) -> bool:
    """Continue a durable image job; each dispatch round has a distinct name."""
    if not (config.GCP_PROJECT and config.SERVICE_URL and config.TASKS_SA_EMAIL):
        return False
    from . import store
    from google.api_core.exceptions import AlreadyExists
    try:
        c = client()
        task = {'name': c.task_path(config.GCP_PROJECT, config.GCP_REGION, config.TASKS_QUEUE,
                                   f'images-{store.url_hash(url)}-r{round_id}'),
                'http_request': {'http_method':tasks_v2.HttpMethod.POST,
                    'url':config.SERVICE_URL.rstrip('/')+'/tasks/images',
                    'headers':{'Content-Type':'application/json'},
                    'body':json.dumps({'url':url}).encode(),
                    'oidc_token':{'service_account_email':config.TASKS_SA_EMAIL,
                                  'audience':config.SERVICE_URL.rstrip('/')}}}
        when = timestamp_pb2.Timestamp()
        when.FromDatetime(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=delay_seconds))
        task['schedule_time']=when
        c.create_task(parent=c.queue_path(config.GCP_PROJECT,config.GCP_REGION,config.TASKS_QUEUE),task=task)
        return True
    except AlreadyExists:
        return True
    except Exception:
        log.warning('image continuation could not be dispatched; durable state retained')
        return False


def enqueue_video(key: str, round_id: int, delay_seconds: int = 1) -> bool:
    """Continue a durable video job on the video queue; one name per round."""
    if not (config.GCP_PROJECT and config.SERVICE_URL and config.TASKS_SA_EMAIL):
        return False
    from . import store
    from google.api_core.exceptions import AlreadyExists
    try:
        c = client()
        task = {'name': c.task_path(config.GCP_PROJECT, config.GCP_REGION, config.VIDEO_TASKS_QUEUE,
                                   f'video-{store.url_hash(key)}-r{round_id}'),
                'http_request': {'http_method': tasks_v2.HttpMethod.POST,
                    'url': config.SERVICE_URL.rstrip('/') + '/tasks/videos',
                    'headers': {'Content-Type': 'application/json'},
                    'body': json.dumps({'key': key}).encode(),
                    'oidc_token': {'service_account_email': config.TASKS_SA_EMAIL,
                                   'audience': config.SERVICE_URL.rstrip('/')}}}
        when = timestamp_pb2.Timestamp()
        when.FromDatetime(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delay_seconds))
        task['schedule_time'] = when
        # A 15-minute Gemini slice outlasts the default 10-minute dispatch deadline.
        task['dispatch_deadline'] = duration_pb2.Duration(seconds=900)
        c.create_task(parent=c.queue_path(config.GCP_PROJECT, config.GCP_REGION, config.VIDEO_TASKS_QUEUE), task=task)
        return True
    except AlreadyExists:
        return True
    except Exception:
        log.warning('video continuation could not be dispatched; durable state retained')
        return False
