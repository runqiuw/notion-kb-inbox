"""The one /tags entry point. Durable, owner-bound menus and explicit replies."""
import copy
import time
import uuid

from google.cloud import firestore

from . import config, notion, store, tag_notion, tag_store, tasks, telegram

PAGE_SIZE = 8
TTL = 3600


def session_ref(chat, message):
    return store.db().collection('ui').document(f'tags_{chat}_{message}')


def prompt_ref(chat, message):
    return store.db().collection('ui').document(f'tag_prompt_{chat}_{message}')


def save(state):
    session_ref(state['chat'], state['message']).set(state)


def valid(state, user):
    return bool(state and state.get('user') == user and state.get('expires', 0) > time.time()
                and not state.get('closed'))


def button(state, text, action, arg=''):
    if action == 'close':
        return text, 'tg:close'
    return text, f"tg:{state['nonce']}:{action}" + (':' + str(arg) if arg != '' else '')


def render(state, text, rows):
    save(state)
    try:
        telegram.edit(state['chat'], state['message'], text[:3900], markup=telegram.keyboard(rows))
    finally:
        # An edit can fail after the new nonce was saved. Release the claim so
        # the still-visible keyboard can recover on the next click.
        @firestore.transactional
        def release(tx):
            ref = session_ref(state['chat'], state['message'])
            current = ref.get(transaction=tx).to_dict()
            if current and current.get('nonce') == state['nonce']:
                tx.set(ref, {'busy_until': 0}, merge=True)
        release(store.db().transaction())
        state['busy_until'] = 0


def menu(state, page=0, mode='list', notice=''):
    data = tag_store.load(strict=True)
    entries = data['entries']
    if mode == 'merge':
        entries = [e for e in entries if e['id'] != state['source']]
    page = max(0, min(page, max(0, (len(entries) - 1) // PAGE_SIZE)))
    state.update(page=page, mode=mode)
    rows = []
    for e in entries[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        prefix = '⏸ ' if not e['enabled'] else ''
        action = 'target' if mode == 'merge' else 'view'
        rows.append([button(state, prefix + e['name'], action, e['id'])])
    paging = []
    if page:
        paging.append(button(state, '◀ 上一页', 'page', page - 1))
    if (page + 1) * PAGE_SIZE < len(entries):
        paging.append(button(state, '下一页 ▶', 'page', page + 1))
    if paging:
        rows.append(paging)
    if mode == 'list':
        rows += [[button(state, '新增／纳管 Notion 标签', 'add')],
                 [button(state, '查看未完成任务', 'jobs')]]
    rows.append([button(state, '取消', 'close')])
    pending = '\n⚠️ 有标签同步未完成，请先查看任务。' if data.get('pending') else ''
    text = ('选择合并目标' if mode == 'merge' else f"正式词表 v{data['version']} · {len(entries)} 项")
    render(state, (notice + '\n' if notice else '') + text + f'（第 {page + 1} 页）' + pending, rows)


def details(state, eid):
    entry = next((e for e in tag_store.load(strict=True)['entries'] if e['id'] == eid), None)
    if not entry:
        return menu(state)
    state.update(source=eid, mode='detail')
    text = f"{entry['name']}\n范围：{entry['scope']}\n别名：{'、'.join(entry['aliases']) or '无'}\n状态：{'启用' if entry['enabled'] else '停用'}"
    rows = [[button(state, '编辑范围和别名', 'edit')],
            [button(state, '停用' if entry['enabled'] else '恢复', 'toggle')],
            [button(state, '改名', 'rename'), button(state, '合并到…', 'merge')],
            [button(state, '返回词表', 'back'), button(state, '取消', 'close')]]
    render(state, text, rows)


def ask(state, action):
    state.update(mode='input', input_action=action)
    descriptions = {
        'add': '需要新增时，请手动选择「回复」这条提示：第一行标签名称，第二行一句范围说明，第三行可选别名（用逗号分隔）。同名 Notion 选项会补充定义后纳管。',
        'edit': '需要编辑时，请手动选择「回复」这条提示：第一行新的范围说明，第二行别名（逗号分隔；空行或「无」表示清空别名）。',
        'rename': '需要改名时，请手动选择「回复」这条提示：新的标签名称。旧名会保留为别名。'}
    render(state, '等待对指定提示的回复。其他消息仍照常收集。', [[button(state, '取消编辑', 'back')]])
    prompt = telegram.send(state['chat'], descriptions[action])
    state['prompt'] = prompt['message_id']
    save(state)
    prompt_ref(state['chat'], prompt['message_id']).set({'menu': state['message'],
                                                       'user': state['user'], 'expires': state['expires']})


def queue_preview(state, change, key):
    job_id = tag_store.new_job(key, change, state['chat'], state['message'], state['user'])
    state.update(mode='job', job=job_id)
    render(state, '正在准备操作预览…', [[('查看进度', 'tg:status:' + job_id)]])
    if not tasks.enqueue_tags(job_id):
        tag_store.put_job(job_id, status='failed', resume_phase='previewing', error='队列不可用')
        render(state, '任务已保存，队列暂不可用。', [[('重试', 'tg:retry:' + job_id)]])


def handle_reply(msg, is_edit=False):
    replied = msg.get('reply_to_message') or {}
    if not replied.get('message_id') or not config.GCP_PROJECT:
        return False
    chat, user = msg['chat']['id'], (msg.get('from') or {}).get('id')
    snap = prompt_ref(chat, replied['message_id']).get()
    if not snap.exists:
        return False
    info = snap.to_dict()
    snap = session_ref(chat, info['menu']).get()
    state = snap.to_dict() if snap.exists else None
    # Recognized prompt replies are always control, including cancelled/stale ones.
    if is_edit or not valid(state, user) or state.get('mode') != 'input' or state.get('prompt') != replied['message_id']:
        telegram.send(chat, '这条标签提示已过期或已取消，请重新打开 /tags。')
        return True
    raw = (msg.get('text') or '').strip()
    if raw.lower() in ('/cancel', '取消'):
        state['nonce'] = uuid.uuid4().hex[:8]
        menu(state)
        return True
    try:
        lines = raw.splitlines()
        action = state['input_action']
        if action == 'add':
            if len(lines) < 2:
                raise ValueError('请按提示输入名称和范围说明，两项分行')
            change = {'action': action, 'id': uuid.uuid4().hex[:12], 'name': lines[0], 'scope': lines[1],
                      'aliases': split_aliases(lines[2] if len(lines) > 2 else '')}
        elif action == 'edit':
            change = {'action': action, 'source': state['source'], 'scope': lines[0] if lines else '',
                      'aliases': split_aliases(lines[1] if len(lines) > 1 else '')}
        else:
            change = {'action': 'rename', 'source': state['source'], 'name': raw}
        tag_store.candidate(tag_store.load(strict=True), change)
        queue_preview(state, change, f"reply:{chat}:{msg['message_id']}")
    except ValueError as e:
        telegram.send(chat, str(e) + '\n请手动回复同一条提示。')
    return True


def split_aliases(text):
    return [] if text.strip() in ('', '无') else [a.strip() for a in text.replace('，', ',').split(',') if a.strip()]


def select_page(state, index):
    target = state['targets'][index]
    state.update(mode='single', target=index, base=target['tags'], selected=list(target['tags']))
    picker(state)


def picker(state, page=0):
    data = tag_store.load(strict=True)
    entries = data['entries']
    labels = [e['name'] for e in entries] + [n for n in state['base'] if n not in tag_store.names(data, enabled=False)]
    state.update(page=page, choices=labels,
                 selection_ids={**state.get('selection_ids', {}), **{e['name']: e['id'] for e in entries}})
    rows = [[button(state, ('☑ ' if n in state['selected'] else '☐ ') + n, 'pick', i)]
            for i, n in enumerate(labels) if page * PAGE_SIZE <= i < (page + 1) * PAGE_SIZE]
    paging = []
    if page:
        paging.append(button(state, '◀ 上一页', 'page', page - 1))
    if (page + 1) * PAGE_SIZE < len(labels):
        paging.append(button(state, '下一页 ▶', 'page', page + 1))
    if paging:
        rows.append(paging)
    rows += [[button(state, '保存', 'save'), button(state, '取消', 'close')]]
    render(state, '勾选标签（只影响所选页面）\n当前：' + ('、'.join(state['selected']) or '无'), rows)


def open_menu(msg):
    chat = msg['chat']['id']
    receipt = store.db().collection('ui').document(f"tag_command_{chat}_{msg['message_id']}")
    client = store.db()
    @firestore.transactional
    def claim(tx):
        snap = receipt.get(transaction=tx)
        if snap.exists:
            d = snap.to_dict()
            if d.get('done'):
                return False, d.get('menu')
            if d.get('lease_until', 0) > time.time():
                raise RuntimeError('标签菜单正在准备')
            tx.set(receipt, {'lease_until': time.time() + 180}, merge=True)
            return True, d.get('menu')
        tx.set(receipt, {'lease_until': time.time() + 180})
        return True, None
    claimed, menu_id = claim(client.transaction())
    if not claimed:
        return
    result = {'message_id': menu_id} if menu_id else telegram.send(chat, '正在读取标签…')
    receipt.set({'menu': result['message_id']}, merge=True)
    state = {'chat': chat, 'message': result['message_id'], 'trigger': msg['message_id'],
             'user': (msg.get('from') or {}).get('id'), 'expires': time.time() + TTL,
             'nonce': uuid.uuid4().hex[:8], 'closed': False}
    replied = msg.get('reply_to_message') or {}
    if replied:
        rec = store.get_record(chat, replied['message_id']) or {}
        targets = []
        if rec.get('notion_page_id'):
            targets.append({'id': rec['notion_page_id'], 'label': '资料' if rec.get('kind') == 'material' else '笔记'})
        attached = rec.get('link_thought') or {}
        if attached.get('id') and attached['id'] not in [t['id'] for t in targets]:
            targets.append({'id': attached['id'], 'label': '附带想法'})
        if not targets:
            render(state, '这条消息尚未入库，暂无页面可编辑。', [[button(state, '关闭', 'close')]])
            receipt.set({'done': True, 'lease_until': 0}, merge=True)
            return
        for target in targets:
            page = notion._call('GET', '/pages/' + target['id'])
            # Restrict to the configured two databases even if a mapping is stale.
            parent = page.get('parent', {})
            dbid = parent.get('database_id', '').replace('-', '')
            if dbid not in {d.replace('-', '') for d in tag_notion.databases()} or page.get('archived') or page.get('in_trash'):
                raise ValueError('映射页面已删除或不属于当前资料／笔记库')
            target['tags'] = tag_notion.page_tags(page)
        state['targets'] = targets
        if len(targets) == 1:
            select_page(state, 0)
        else:
            state['mode'] = 'targets'
            render(state, '同一条消息对应多个页面，请先选择：',
                   [[button(state, t['label'], 'which', i)] for i, t in enumerate(targets)] + [[button(state, '取消', 'close')]])
    else:
        menu(state)
    receipt.set({'done': True, 'lease_until': 0}, merge=True)


def owned_job(job_id, user, chat, message):
    job = tag_store.get_job(job_id)
    if not job or job.get('user_id') != user or job.get('chat_id') != chat or job.get('message_id') != message:
        return None
    return job


def handle_callback(cb, *, acknowledge=True):
    if acknowledge:
        telegram.answer_callback(cb['id'])
    parts = (cb.get('data') or '').split(':')
    msg = cb.get('message') or {}
    chat, mid = (msg.get('chat') or {}).get('id'), msg.get('message_id')
    user = (cb.get('from') or {}).get('id')
    if not chat or not mid or len(parts) < 2:
        return
    action = parts[1]
    job_actions = ('confirm', 'cancel', 'status', 'retry', 'navretry', 'navdone')
    if action in job_actions and len(parts) == 3:
        job_id = parts[2]
        job = owned_job(job_id, user, chat, mid)
        if not job:
            return
        if action == 'confirm' and tag_store.confirm(job_id):
            telegram.edit(chat, mid, '已确认，正在同步…', markup=telegram.keyboard([[('查看进度', 'tg:status:' + job_id)]]))
            if not tasks.enqueue_tags(job_id, 'apply'):
                tag_store.put_job(job_id, status='failed', resume_phase='queued', error='队列不可用')
                telegram.edit(chat, mid, '队列不可用，任务已保存。', markup=telegram.keyboard([[('重试', 'tg:retry:' + job_id)]]))
        elif action == 'cancel':
            tag_store.confirm(job_id, cancel=True)
            if (tag_store.get_job(job_id) or {}).get('status') == 'cancelled':
                telegram.edit(chat, mid, '已取消。', markup=telegram.keyboard([]))
        elif action in ('retry', 'navretry'):
            # Atomic state transition: duplicate deliveries/clicks enqueue once.
            client = store.db()
            @firestore.transactional
            def retry(tx):
                current = tag_store.job_ref(job_id).get(transaction=tx).to_dict()
                if action == 'retry' and current['status'] == 'failed':
                    status = current.get('resume_phase', 'syncing')
                elif action == 'navretry' and current['status'] == 'needs_navigation':
                    status = 'navigation_retry'
                else:
                    return False
                tx.set(tag_store.job_ref(job_id), {'status': status, 'error': '', 'conflict': False}, merge=True)
                return True
            if retry(client.transaction()):
                if not tasks.enqueue_tags(job_id, uuid.uuid4().hex[:12]):
                    tag_store.put_job(job_id, status='failed', resume_phase=job.get('resume_phase', 'navigation_retry'), error='队列不可用')
        elif action == 'navdone' and job['status'] == 'needs_navigation':
            tag_store.put_job(job_id, status='done', navigation_confirmed_by=user,
                              navigation_confirmed_at=time.time())
            telegram.edit(chat, mid, '✅ 两库与词表已同步，导航收尾已由你确认完成。', markup=telegram.keyboard([]))
        elif action == 'status':
            note = f"任务状态：{job['status']}；已处理 {(job.get('progress') or {}).get('migrated', 0)} 条页面。"
            rows = [[('重试', 'tg:retry:' + job_id)]] if job['status'] == 'failed' else [[('刷新', 'tg:status:' + job_id)]]
            telegram.send(chat, note + ('\n' + job['error'] if job.get('error') else ''))
            # Keep original confirmation/navigation controls intact.
        return
    # Accept both the new stable close button and previously sent keyboards.
    if (action == 'close' and len(parts) == 2) or (len(parts) >= 3 and parts[2] == 'close'):
        snap = session_ref(chat, mid).get()
        state = snap.to_dict() if snap.exists else None
        if not state or state.get('user') != user:
            return
        state['closed'] = True
        save(state)
        telegram.edit(chat, mid, '已关闭。任务进度仍可通过 /tags 查看。', markup=telegram.keyboard([]))
        return
    if len(parts) < 3:
        return
    nonce, action = parts[1:3]
    arg = parts[3] if len(parts) > 3 else ''
    client = store.db()
    @firestore.transactional
    def claim(tx):
        ref = session_ref(chat, mid)
        snap = ref.get(transaction=tx)
        state = snap.to_dict() if snap.exists else None
        if not state or state.get('user') != user:
            return None, ''
        if state.get('closed'):
            return state, 'closed'
        if state.get('busy_until', 0) > time.time() or state.get('last_callback') == cb['id']:
            return None, ''
        recover = not valid(state, user) or state['nonce'] != nonce
        state['nonce'] = uuid.uuid4().hex[:8]
        state.update(expires=time.time() + TTL, busy_until=time.time() + 35,
                     last_callback=cb['id'])
        tx.set(ref, state)
        return state, 'refresh' if recover else ''
    state, recovery = claim(client.transaction())
    if not state:
        return
    if recovery == 'closed':
        telegram.edit(chat, mid, '已关闭。请用 /tags 打开新菜单。', markup=telegram.keyboard([]))
        return
    try:
        if recovery == 'refresh':
            # Never replay a stale mutation against a newer selection/state.
            menu(state, notice='菜单已刷新，请重新选择操作。')
        elif action == 'back':
            menu(state)
        elif action == 'page':
            if state['mode'] == 'single':
                picker(state, int(arg))
            else:
                menu(state, int(arg), state['mode'])
        elif action == 'view':
            details(state, arg)
        elif action in ('add', 'edit', 'rename'):
            ask(state, action)
        elif action == 'toggle':
            queue_preview(state, {'action': 'toggle', 'source': state['source']}, 'callback:' + cb['id'])
        elif action == 'merge':
            menu(state, mode='merge')
        elif action == 'target':
            queue_preview(state, {'action': 'merge', 'source': state['source'], 'target': arg}, 'callback:' + cb['id'])
        elif action == 'which':
            select_page(state, int(arg))
        elif action == 'pick':
            label = state['choices'][int(arg)]
            if label in state['selected']:
                state['selected'].remove(label)
            else:
                state['selected'].append(label)
            picker(state, state['page'])
        elif action == 'save':
            target = state['targets'][state['target']]
            queue_preview(state, {'action': 'single', 'page_id': target['id'], 'base': state['base'],
                                  'selected': state['selected'], 'selection_ids': state['selection_ids']}, 'callback:' + cb['id'])
        elif action == 'jobs':
            jobs = []
            for snap in store.db().collection('tag_operations').where('user_id', '==', user).stream():
                job = snap.to_dict()
                if job.get('chat_id') == chat and job['status'] not in ('done', 'cancelled'):
                    jobs.append((snap.id, job))
            jobs.sort(key=lambda pair: pair[1].get('created_at', 0), reverse=True)
            rows = [[button(state, f"{j['change']['action']} · {j['status']}", 'job', jid)] for jid, j in jobs[:8]]
            render(state, '未完成的标签任务：' if jobs else '没有未完成任务。', rows + [[button(state, '返回', 'back')]])
        elif action == 'job':
            job = tag_store.get_job(arg)
            if job and job['user_id'] == user and job['chat_id'] == chat:
                telegram.send(chat, f"任务状态：{job['status']}。请在原操作消息继续。")
                # Recovery works even if the old menu was closed.
                from . import tag_jobs
                if job['status'] == 'needs_navigation':
                    tag_jobs.finish(arg, job)
                elif job['status'] == 'ready':
                    telegram.edit(chat, job['message_id'], job['preview_text'], markup=telegram.keyboard([[('确认保存', 'tg:confirm:' + arg), ('取消', 'tg:cancel:' + arg)]]))
                elif job['status'] == 'failed':
                    telegram.edit(chat, job['message_id'], '任务未完成：' + job.get('error', ''),
                                  markup=telegram.keyboard([[('重试', 'tg:retry:' + arg)]]))
    except (ValueError, IndexError, KeyError) as e:
        render(state, str(e) + '\n请重新打开 /tags。', [[button(state, '返回词表', 'back')]])
