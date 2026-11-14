"""Versioned formal vocabulary and durable tag jobs (Firestore is authoritative)."""
import copy
import hashlib
import logging
import time
import uuid
from contextlib import contextmanager

from google.cloud import firestore

from . import config, store, topics

log = logging.getLogger(__name__)
_CACHE = None


def ref():
    return store.db().collection('settings').document('tags')


def seed():
    entries = []
    for group in topics.GROUPS.values():
        for name, scope in group.items():
            entries.append({'id': hashlib.sha256(name.encode()).hexdigest()[:12],
                            'name': name, 'scope': scope, 'enabled': True,
                            'aliases': [k for k, v in topics._ALIASES.items() if v == name]})
    return {'version': 1, 'entries': entries, 'pending': None}


def initialize():
    client = store.db()
    @firestore.transactional
    def init(tx):
        snap = ref().get(transaction=tx)
        if snap.exists:
            return snap.to_dict()
        first = client.collection('tag_versions').document('1').get(transaction=tx)
        if first.exists:
            raise RuntimeError('正式词表缺失但初始化历史仍存在，请从备份恢复；不会重新启用内置词表')
        data = seed()
        tx.set(ref(), data)
        tx.set(client.collection('tag_versions').document('1'), data)
        return data
    return init(client.transaction())


def load(strict=False):
    """Read version on every use; never re-seed an initialized vocabulary on error.

    Prompt reads may fall back to the last successful snapshot. Commits must
    read Firestore successfully, including pending changes, or fail closed.
    Local offline runs without a GCP project use the explicit seed fixture.
    """
    global _CACHE
    if not config.GCP_PROJECT:
        return copy.deepcopy(_CACHE or seed())
    try:
        snap = ref().get()
        data = snap.to_dict() if snap.exists else initialize()
        _CACHE = copy.deepcopy(data)
        return data
    except Exception:
        if strict or _CACHE is None:
            raise
        log.warning('tag vocabulary read failed; using last successful version')
        return copy.deepcopy(_CACHE)


def invalidate():
    global _CACHE
    _CACHE = None


def names(data=None, enabled=True):
    data = data if data is not None else load()
    return [e['name'] for e in data['entries'] if not enabled or e['enabled']]


def normalize(values, data=None, limit=3, enabled=True, writing=False):
    data = data if data is not None else load(strict=writing)
    blocked = set((data.get('pending') or {}).get('blocked_ids', [])) if writing else set()
    lookup = {}
    for e in data['entries']:
        if (enabled and not e['enabled']) or e['id'] in blocked:
            continue
        for label in [e['name'], *e.get('aliases', [])]:
            lookup[topics.key(label)] = e['name']
    selected = list(dict.fromkeys(lookup.get(topics.key(t), '')
                                 for t in values if isinstance(t, str)))
    selected = [t for t in selected if t]
    if 'MLSys' in selected and 'Systems' in selected and enabled:
        selected.remove('Systems')
    return selected[:limit] if limit is not None else selected


def validate_entries(entries):
    seen = {}
    for entry in entries:
        entry['name'] = topics.clean(entry['name'])
        entry['scope'] = entry['scope'].strip()
        if not entry['name'] or len(entry['name']) > 80 or any(c in entry['name'] for c in ',\n\r'):
            raise ValueError('标签名称需为 1–80 字，不能含逗号或换行')
        if not entry['scope'] or len(entry['scope']) > 500:
            raise ValueError('范围说明需为 1–500 字')
        aliases = [topics.clean(a) for a in entry.get('aliases', []) if topics.clean(a)]
        if len(aliases) > 20 or any(len(a) > 80 or ',' in a for a in aliases):
            raise ValueError('别名最多 20 个，每个最多 80 字且不能含逗号')
        entry['aliases'] = list(dict.fromkeys(a for a in aliases if topics.key(a) != topics.key(entry['name'])))
        for name in [entry['name'], *entry['aliases']]:
            k = topics.key(name)
            if k in seen and seen[k] != entry['id']:
                raise ValueError(f'名称或别名冲突：{name}')
            seen[k] = entry['id']


def candidate(data, change):
    entries = copy.deepcopy(data['entries'])
    by_id = {e['id']: e for e in entries}
    action = change['action']
    source = by_id.get(change.get('source'))
    if action == 'add':
        entries.append({'id': change['id'], 'name': change['name'], 'scope': change['scope'],
                        'aliases': change.get('aliases', []), 'enabled': True})
    elif source is None:
        raise ValueError('标签已改变，请重新打开 /tags')
    elif action == 'edit':
        source.update(scope=change['scope'], aliases=change['aliases'])
    elif action == 'toggle':
        source['enabled'] = not source['enabled']
    elif action == 'rename':
        old = source['name']
        if topics.key(old) == topics.key(change['name']):
            raise ValueError(f'新名称与「{old}」相同（大小写不区分）；请使用不同名称')
        source['name'] = change['name']
        source['aliases'].append(old)
    elif action == 'merge':
        target = by_id.get(change['target'])
        if target is None or target['id'] == source['id']:
            raise ValueError('请选择不同的目标标签')
        target['aliases'] += [source['name'], *source['aliases']]
        entries.remove(source)
    else:
        raise ValueError('不支持的操作')
    validate_entries(entries)
    return entries


@contextmanager
def write_guard():
    """Serialize vocabulary transitions with tag-bearing Notion commits.

    A pending rename/disable cannot race an already validated page create.
    The lease exceeds a single Notion request timeout; long jobs use one lease
    per page, never hold it across full database scans.
    """
    if not config.GCP_PROJECT:
        yield
        return
    client = store.db()
    lease = client.collection('maintenance').document('tag_write')
    owner = uuid.uuid4().hex
    @firestore.transactional
    def claim(tx):
        snap = lease.get(transaction=tx)
        if snap.exists and (snap.to_dict() or {}).get('expires_at', 0) > time.time():
            raise RuntimeError('标签正在同步，请稍后重试')
        tx.set(lease, {'owner': owner, 'expires_at': time.time() + 90})
    claim(client.transaction())
    try:
        yield
    finally:
        @firestore.transactional
        def release(tx):
            snap = lease.get(transaction=tx)
            if snap.exists and snap.to_dict().get('owner') == owner:
                tx.delete(lease)
        release(client.transaction())


def job_ref(job_id):
    return store.db().collection('tag_operations').document(job_id)


def get_job(job_id):
    snap = job_ref(job_id).get()
    return snap.to_dict() if snap.exists else None


def put_job(job_id, **fields):
    fields['updated_at'] = time.time()
    job_ref(job_id).set(fields, merge=True)


def new_job(key, change, chat_id, message_id, user_id):
    job_id = hashlib.sha256(key.encode()).hexdigest()[:24]
    client = store.db()
    @firestore.transactional
    def create(tx):
        snap = job_ref(job_id).get(transaction=tx)
        if not snap.exists:
            tx.set(job_ref(job_id), {'change': change, 'chat_id': chat_id,
                   'message_id': message_id, 'user_id': user_id, 'status': 'previewing',
                   'base_version': load(strict=True)['version'], 'created_at': time.time()})
    create(client.transaction())
    return job_id


def claim_job(job_id):
    client = store.db()
    owner = uuid.uuid4().hex
    @firestore.transactional
    def claim(tx):
        snap = job_ref(job_id).get(transaction=tx)
        if not snap.exists:
            return None
        job = snap.to_dict()
        if job.get('status') in ('done', 'cancelled', 'ready', 'needs_navigation') or (job.get('status') == 'failed' and job.get('conflict')):
            return None
        if job.get('lease_until', 0) > time.time():
            raise RuntimeError('标签任务正在执行')
        tx.set(job_ref(job_id), {'lease_owner': owner, 'lease_until': time.time() + 240}, merge=True)
        return job
    return owner, claim(client.transaction())


def release_job(job_id, owner):
    client = store.db()
    @firestore.transactional
    def release(tx):
        snap = job_ref(job_id).get(transaction=tx)
        if snap.exists and snap.to_dict().get('lease_owner') == owner:
            tx.set(job_ref(job_id), {'lease_until': 0}, merge=True)
    release(client.transaction())


def confirm(job_id, cancel=False):
    client = store.db()
    @firestore.transactional
    def confirm_tx(tx):
        snap = job_ref(job_id).get(transaction=tx)
        if not snap.exists:
            return False
        job = snap.to_dict()
        if job['status'] != 'ready':
            return False
        tx.set(job_ref(job_id), {'status': 'cancelled' if cancel else 'queued'}, merge=True)
        return not cancel
    return confirm_tx(client.transaction())


def stage(job_id, job):
    """One outstanding schema change, and an immutable previous version."""
    client = store.db()
    with write_guard():
        @firestore.transactional
        def stage_tx(tx):
            snap = ref().get(transaction=tx)
            data = snap.to_dict()
            pending = data.get('pending')
            if pending:
                if pending['operation'] == job_id:
                    return pending
                raise ValueError('另一项标签同步未完成，请先重试该任务')
            if data['version'] != job['base_version']:
                raise ValueError('词表版本已改变，请重新打开 /tags 并预览')
            entries = candidate(data, job['change'])
            change = job['change']
            blocked = [change['source']] if change['action'] in ('rename', 'merge', 'toggle') else []
            pending = {'operation': job_id, 'entries': entries, 'version': data['version'] + 1,
                       'blocked_ids': blocked}
            tx.set(ref(), {'pending': pending}, merge=True)
            tx.set(job_ref(job_id), {'status': 'syncing', 'previous': data,
                                    'pending_version': pending['version']}, merge=True)
            return pending
        result = stage_tx(client.transaction())
    return result


def publish(job_id):
    client = store.db()
    with write_guard():
        @firestore.transactional
        def commit(tx):
            data = ref().get(transaction=tx).to_dict()
            pending = data.get('pending') or {}
            if pending.get('operation') != job_id:
                raise ValueError('待同步版本已改变')
            new = {'version': pending['version'], 'entries': pending['entries'], 'pending': None}
            tx.set(ref(), new)
            tx.set(client.collection('tag_versions').document(str(new['version'])), new)
            tx.set(job_ref(job_id), {'published_version': new['version']}, merge=True)
        commit(client.transaction())
