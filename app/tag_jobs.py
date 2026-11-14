"""Bounded, retryable tag management workers; no scans in button webhooks."""
import time
import uuid

from . import tag_store, tag_notion, tasks, telegram


def labels(job):
    change = job['change']
    entries = {e['id']: e for e in job['previous']['entries']}
    old = entries[change['source']]['name']
    new = change['name'] if change['action'] == 'rename' else entries[change['target']]['name']
    return old, new


def notify(job, text, rows):
    try:
        telegram.edit(job['chat_id'], job['message_id'], text[:3900], markup=telegram.keyboard(rows))
    except Exception:
        # Durable task state is authoritative; /tags can reopen controls.
        import logging
        logging.getLogger(__name__).warning('could not update tag task message')


def preview(job_id, job):
    data = tag_store.load(strict=True)
    change = job['change']
    action = change['action']
    if action != 'single':
        if data.get('pending'):
            raise ValueError('另一项标签同步未完成，请先重试该任务')
        entries = tag_store.candidate(data, change)
        tag_notion.check_option_spelling(entries)
    else:
        entries = data['entries']
    fields = {'base_version': data['version'], 'previous': data, 'status': 'ready'}
    if action in ('rename', 'merge'):
        old, new = labels({**job, 'previous': data})
        counts = [tag_notion.count(db, old) for db in tag_notion.databases()]
        nav = tag_notion.discover_navigation(old, new)
        fields.update(counts=counts, navigation=nav)
        text = f"预览：{old} → {new}\n涉及资料 {counts[0]} 条、笔记 {counts[1]} 条、视图 {len(nav['views'])} 个、导航页 {len(nav['pages'])} 个。\n保留其他标签、正文及阅读状态。"
        if nav['manual']:
            text += '\n导航收尾（完成后需确认）：\n' + '\n'.join(nav['manual'])
    elif action == 'single':
        tag_notion.notion._call('GET', '/pages/' + change['page_id'])
        text = '仅修改所选页面标签：\n' + ('、'.join(change['selected']) or '（清空）')
    else:
        entry_id = change.get('source', change.get('id'))
        entry = next(e for e in entries if e['id'] == entry_id)
        text = f"预览：{entry['name']}\n范围：{entry['scope']}\n别名：{'、'.join(entry['aliases']) or '无'}\n状态：{'启用' if entry['enabled'] else '停用'}\n既有页面不自动重标。"
    # Durable preview before making the confirm button visible.
    fields['preview_text'] = text
    tag_store.put_job(job_id, **fields)
    notify(job, text, [[('确认保存', 'tg:confirm:' + job_id), ('取消', 'tg:cancel:' + job_id)]])


def apply(job_id, job, deadline):
    change = job['change']
    action = change['action']
    if action == 'single':
        def transform(old):
            data = tag_store.load(strict=True)
            # Historical unmanaged/disabled tags may be retained, never invented.
            chosen = []
            base = []
            lookup = {e['id']: e for e in data['entries']}
            pending_blocked = set((data.get('pending') or {}).get('blocked_ids', []))
            for name in change['base']:
                eid = change.get('selection_ids', {}).get(name)
                if eid and (eid not in lookup or eid in pending_blocked):
                    raise ValueError('词表已改变或正在同步，请重新打开 /tags 编辑这条页面')
                base.append(lookup[eid]['name'] if eid else name)
            for name in change['selected']:
                eid = change.get('selection_ids', {}).get(name)
                if eid:
                    if eid in pending_blocked:
                        raise ValueError('该标签正在同步，请完成后重新编辑')
                    if eid not in lookup:
                        raise ValueError('词表已改变，请重新编辑标签')
                    chosen.append(lookup[eid]['name'])
                elif name in change['base']:
                    chosen.append(name)
            return tag_notion.selected_delta(old, base, chosen)
        tag_notion.patch_tags(change['page_id'], transform)
        tag_store.put_job(job_id, status='done')
        notify(job, '✅ 已保存这一个页面的标签。', [[('关闭', 'tg:close')]])
        return
    if not job.get('pending_version'):
        pending = tag_store.stage(job_id, job)
        job = tag_store.get_job(job_id)
    else:
        pending = tag_store.load(strict=True).get('pending')
        if not pending or pending['operation'] != job_id:
            if job.get('published_version'):
                finish(job_id, job)
                return
            raise ValueError('待同步配置已改变')
    entries = pending['entries']
    dbs = tag_notion.databases()
    progress = dict(job.get('progress') or {})
    # Add destinations before moving references. Keep legacy/Notion-only options.
    for i, db in enumerate(dbs):
        key = f'options_{i}'
        if not progress.get(key):
            tag_notion.sync_options(db, entries)
            progress[key] = True
            tag_store.put_job(job_id, progress=progress)
    if action in ('rename', 'merge'):
        old, new = labels(job)
        for i, db in enumerate(dbs):
            key = f'pages_{i}'
            if progress.get(key):
                continue
            # Query from the beginning: migrated pages leave the source filter.
            while time.monotonic() < deadline:
                pages = tag_notion.query(db, old, limit=20).get('results', [])
                if not pages:
                    progress[key] = True
                    tag_store.put_job(job_id, progress=progress)
                    break
                for page in pages:
                    if time.monotonic() >= deadline:
                        return continue_job(job_id, job, progress)
                    tag_notion.patch_tags(page['id'], lambda ns: [new if n == old else n for n in ns])
                    progress['migrated'] = progress.get('migrated', 0) + 1
                    tag_store.put_job(job_id, progress=progress)
            if not progress.get(key):
                return continue_job(job_id, job, progress)
        for i, db in enumerate(dbs):
            key = f'cleanup_{i}'
            if not progress.get(key):
                tag_notion.sync_options(db, entries, remove=old)
                progress[key] = True
                tag_store.put_job(job_id, progress=progress)
        if not progress.get('navigation'):
            tag_notion.sync_navigation(job['navigation'], old, new)
            progress['navigation'] = True
            tag_store.put_job(job_id, progress=progress)
    # Publication waits for both schemas and all migrations. A failed request
    # resumes from durable checkpoints, retaining the previous model version.
    if not job.get('published_version'):
        tag_store.publish(job_id)
    finish(job_id, tag_store.get_job(job_id))


def continue_job(job_id, job, progress):
    tag_store.put_job(job_id, status='syncing', progress=progress)
    if not tasks.enqueue_tags(job_id, uuid.uuid4().hex[:12]):
        raise RuntimeError('队列不可用，进度已保存，请重试')
    notify(job, f"正在同步，已处理 {progress.get('migrated', 0)} 条页面…", [[('查看进度', 'tg:status:' + job_id)]])


def finish(job_id, job):
    manual = (job.get('navigation') or {}).get('manual') or []
    status = 'needs_navigation' if manual else 'done'
    tag_store.put_job(job_id, status=status)
    if manual:
        notify(job, '两库和词表已同步；导航收尾尚未完成：\n' + '\n'.join(manual),
               [[('重试自动导航同步', 'tg:navretry:' + job_id)],
                [('我已完成导航收尾', 'tg:navdone:' + job_id)]])
    else:
        notify(job, '✅ 标签已保存，两库与导航同步完成。', [[('关闭', 'tg:close')]])


def process(job_id):
    owner, job = tag_store.claim_job(job_id)
    if not job:
        return
    phase = job.get('resume_phase', 'syncing') if job['status'] == 'failed' else job['status']
    if job['status'] == 'failed' and job.get('conflict'):
        return
    try:
        if phase == 'previewing':
            preview(job_id, job)
        elif phase == 'navigation_retry':
            old, new = labels(job)
            nav = tag_notion.discover_navigation(old, new)
            tag_notion.sync_navigation(nav, old, new)
            tag_store.put_job(job_id, navigation=nav)
            finish(job_id, tag_store.get_job(job_id))
        elif phase in ('queued', 'syncing'):
            apply(job_id, job, time.monotonic() + 180)
    except Exception as e:
        tag_store.put_job(job_id, status='failed', resume_phase=phase, error=telegram.redact(e), conflict=isinstance(e, ValueError))
        notify(job, '⚠️ 标签任务未完成，进度已保留。\n' + telegram.redact(e),
               [[('重试', 'tg:retry:' + job_id)], [('关闭', 'tg:close')]])
        raise
    finally:
        tag_store.release_job(job_id, owner)
