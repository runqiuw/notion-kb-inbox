"""Isolated tag lifecycle, concurrency, partial sync and Telegram routing tests."""
import copy
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, enrich, chat_notes, tag_jobs, tag_notion, tag_store, tag_ui, topics, pipeline, main


class Snap:
    def __init__(self, data, path):
        self.id = path.rsplit('/', 1)[-1]
        self.exists = data is not None
        self.data = copy.deepcopy(data)
    def to_dict(self):
        return copy.deepcopy(self.data)


class Ref:
    def __init__(self, db, path):
        self.db, self.path = db, path
    def get(self, transaction=None):
        if self.db.fail:
            raise RuntimeError('Firestore offline')
        return Snap(self.db.data.get(self.path), self.path)
    def set(self, data, merge=False):
        if merge:
            self.db.data.setdefault(self.path, {}).update(copy.deepcopy(data))
        else:
            self.db.data[self.path] = copy.deepcopy(data)
    def delete(self):
        self.db.data.pop(self.path, None)


class Collection:
    def __init__(self, db, path, condition=None):
        self.db, self.path, self.condition = db, path, condition
    def document(self, name):
        return Ref(self.db, self.path + '/' + name)
    def where(self, field, op, value):
        return Collection(self.db, self.path, (field, value))
    def stream(self):
        for path, data in list(self.db.data.items()):
            if path.startswith(self.path + '/') and (not self.condition or data.get(self.condition[0]) == self.condition[1]):
                yield Snap(data, path)


class Tx:
    def set(self, ref, data, merge=False):
        ref.set(data, merge)
    def delete(self, ref):
        ref.delete()


class DB:
    def __init__(self):
        self.data, self.fail = {}, False
    def collection(self, path):
        return Collection(self, path)
    def transaction(self):
        return Tx()


class Notion:
    def __init__(self):
        self.pages = {}
        self.schemas = {d: {'properties': {'主题': {'id': 'tags', 'multi_select': {'options': [{'name': n, 'id': n, 'color': 'blue'} for n in topics.TOPICS]}}}} for d in ('materials', 'thoughts')}
        self.fail = None
        self.calls = []
    def call(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if self.fail == (method, path):
            self.fail = None
            raise RuntimeError('injected partial failure')
        parts = path.strip('/').split('/')
        if parts[0] == 'databases':
            db = parts[1]
            if method == 'PATCH':
                prior = {o['id']: o['name'] for o in self.schemas[db]['properties']['主题']['multi_select']['options'] if o.get('id')}
                for option in payload['properties']['主题']['multi_select']['options']:
                    old = prior.get(option.get('id'))
                    if old and old != option['name']:
                        for page in self.pages.values():
                            if page['parent']['database_id'] == db:
                                for tag in page['properties']['主题']['multi_select']:
                                    if tag['name'] == old:
                                        tag['name'] = option['name']
                self.schemas[db]['properties']['主题'].update(copy.deepcopy(payload['properties']['主题']))
                return copy.deepcopy(self.schemas[db])
            if method == 'GET':
                return copy.deepcopy(self.schemas[db])
            label = payload['filter']['multi_select']['contains']
            pages = [p for p in self.pages.values() if p['parent']['database_id'] == db and label in tag_notion.page_tags(p)]
            return {'results': copy.deepcopy(pages[:payload['page_size']]), 'has_more': False}
        if parts[0] == 'pages':
            if method == 'POST':
                pid = str(len(self.pages) + 1)
                self.pages[pid] = {'id': pid, 'url': 'https://notion.test/' + pid, **copy.deepcopy(payload)}
                return copy.deepcopy(self.pages[pid])
            page = self.pages[parts[1]]
            if method == 'PATCH':
                page['properties'].update(copy.deepcopy(payload['properties']))
            return copy.deepcopy(page)
        raise AssertionError((method, path))
    def add(self, pid, db, labels):
        self.pages[pid] = {'id': pid, 'parent': {'database_id': db}, 'properties': {'主题': {'multi_select': [{'name': n} for n in labels]}, '阅读状态': {'select': {'name': '已读'}}}, 'children': [{'body': 'untouched'}]}


class Tests(unittest.TestCase):
    def setUp(self):
        self.db, self.api = DB(), Notion()
        self.sent, self.edits, self.answers, self.queued = [], [], [], []
        self.patches = [patch.object(config, 'GCP_PROJECT', 'isolated'),
                        patch.object(config, 'NOTION_MATERIAL_DB', 'materials'),
                        patch.object(config, 'NOTION_THOUGHT_DB', 'thoughts'),
                        patch.object(tag_store.store, 'db', lambda: self.db),
                        patch.object(tag_store.firestore, 'transactional', lambda f: f),
                        patch.object(tag_notion.notion, '_call', self.api.call),
                        patch.object(telegram := tag_ui.telegram, 'send', self.send),
                        patch.object(telegram, 'edit', lambda *a, **k: self.edits.append((a, k))),
                        patch.object(telegram, 'answer_callback', lambda *a: self.answers.append(a)),
                        patch.object(tag_ui.tasks, 'enqueue_tags', lambda *a: self.queued.append(a) or True),
                        patch.object(tag_notion, 'discover_navigation', lambda old, new: {'views': [], 'pages': [], 'manual': []})]
        for p in self.patches:
            p.start()
        tag_store.invalidate()
        tag_store.initialize()
    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        tag_store.invalidate()
    def send(self, *a, **kw):
        self.sent.append((a, kw))
        return {'message_id': 500 + len(self.sent)}
    def entry(self, name):
        return next(e for e in tag_store.load()['entries'] if e['name'] == name)
    def new(self, change, key='test'):
        return tag_store.new_job(key, change, 1, 501, 7)
    def perform(self, change, key='test'):
        jid = self.new(change, key)
        tag_jobs.process(jid)
        self.assertTrue(tag_store.confirm(jid))
        tag_jobs.process(jid)
        self.assertEqual(tag_store.get_job(jid)['status'], 'done')
        return jid
    def msg(self, mid=42, reply=None, text='/tags'):
        result = {'chat': {'id': 1}, 'from': {'id': 7}, 'message_id': mid, 'text': text}
        if reply:
            result['reply_to_message'] = {'message_id': reply}
        return result
    def cb(self, data, mid=501, user=7, key='cb'):
        return {'id': key, 'data': data, 'from': {'id': user}, 'message': {'chat': {'id': 1}, 'message_id': mid}}

    def test_seed_and_unknown(self):
        self.assertEqual(len(tag_store.names()), 24)
        self.assertEqual(enrich._cap_topics(['AI Agent', 'Agent', 'Guide', 'unknown', 'AI']), ['Agent', 'Tutorial'])
        self.assertNotIn('FromNotion', topics.vocabulary(['FromNotion']))
        self.assertEqual(tag_store.normalize(['Systems', 'MLSys', 'Infra', 'Data']), ['MLSys', 'Infra', 'Data'])

    def test_missing_initialized_config_does_not_reseed(self):
        self.db.data.pop('settings/tags')
        with self.assertRaisesRegex(RuntimeError, '初始化历史'):
            tag_store.initialize()
        self.assertNotIn('settings/tags', self.db.data)

    def test_add_define_alias_conflict_and_manual_option(self):
        jid = self.perform({'action': 'add', 'id': 'robotics', 'name': 'Robotics', 'scope': '具身机器人实验', 'aliases': ['机器人']})
        self.assertIn('Robotics：具身机器人实验', topics.prompt())
        self.assertEqual(enrich._cap_topics(['机器人', 'unknown']), ['Robotics'])
        for schema in self.api.schemas.values():
            self.assertIn('Robotics', [o['name'] for o in schema['properties']['主题']['multi_select']['options']])
        with self.assertRaises(ValueError):
            tag_store.candidate(tag_store.load(), {'action': 'add', 'id': 'bad', 'name': 'foo', 'scope': 'bar', 'aliases': ['ai agent']})
        self.assertEqual(tag_store.get_job(jid)['published_version'], 2)

    def test_pending_disable_fallback_and_new_writes(self):
        source = self.entry('Agent')['id']
        jid = self.new({'action': 'toggle', 'source': source})
        tag_jobs.process(jid)
        tag_store.confirm(jid)
        job = tag_store.get_job(jid)
        tag_store.stage(jid, job)
        self.assertEqual(tag_store.normalize(['Agent'], writing=True), [])
        self.api.add('existing', 'materials', ['Agent'])
        tag_jobs.process(jid)
        self.assertEqual(tag_notion.page_tags(self.api.pages['existing']), ['Agent'])
        self.assertNotIn('Agent', tag_store.names())
        tag_store.load()
        self.db.fail = True
        self.assertNotIn('Agent', tag_store.names())
        with self.assertRaises(RuntimeError):
            tag_store.normalize(['RL'], writing=True)
        self.db.fail = False
        tag_store.invalidate()
        self.assertNotIn('Agent', tag_store.names())
        self.perform({'action': 'toggle', 'source': source}, key='restore')
        self.assertIn('Agent', tag_store.names())
        self.assertEqual(tag_store.initialize()['version'], 3)

    def test_case_conflicts_reject_before_staging_and_exact_option_adoption(self):
        with self.assertRaises(ValueError):
            tag_store.candidate(tag_store.load(), {'action': 'rename', 'source': self.entry('Agent')['id'], 'name': 'agent'})
        for schema in self.api.schemas.values():
            schema['properties']['主题']['multi_select']['options'].append({'id': 'manual', 'name': 'robotics'})
        jid = self.new({'action': 'add', 'id': 'robotics', 'name': 'Robotics', 'scope': '机器人范围'}, key='case-conflict')
        with self.assertRaisesRegex(ValueError, 'Notion 已有同名'):
            tag_jobs.process(jid)
        self.assertIsNone(tag_store.load()['pending'])
        self.perform({'action': 'add', 'id': 'robotics', 'name': 'robotics', 'scope': '机器人范围'}, key='adopt')
        for schema in self.api.schemas.values():
            options = schema['properties']['主题']['multi_select']['options']
            self.assertEqual([o for o in options if o.get('id') == 'manual'], [{'id': 'manual', 'name': 'robotics'}])

    def test_partial_two_database_failure_resume(self):
        jid = self.new({'action': 'add', 'id': 'x', 'name': 'X', 'scope': 'X范围'})
        tag_jobs.process(jid)
        tag_store.confirm(jid)
        self.api.fail = ('PATCH', '/databases/thoughts')
        with self.assertRaises(RuntimeError):
            tag_jobs.process(jid)
        self.assertEqual(tag_store.load()['version'], 1)
        self.assertTrue(tag_store.load()['pending'])
        self.assertTrue(tag_store.get_job(jid)['progress']['options_0'])
        tag_ui.handle_callback(self.cb('tg:retry:' + jid))
        tag_jobs.process(jid)
        self.assertEqual(tag_store.load()['version'], 2)
        self.assertIsNone(tag_store.load()['pending'])
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'PATCH' and c[1] == '/databases/materials']), 1)

    def test_rename_and_merge_preserve_everything(self):
        self.api.add('m', 'materials', ['Agent', 'RL', 'Data', 'Tool', 'Legacy', 'Infra'])
        self.api.add('t', 'thoughts', ['Agent', 'RL', 'Research'])
        before = copy.deepcopy(self.api.pages)
        self.perform({'action': 'rename', 'source': self.entry('Agent')['id'], 'name': 'AI Agents'})
        self.assertEqual(tag_store.normalize(['Agent', '智能体']), ['AI Agents'])
        self.assertIn('AI Agents', tag_notion.page_tags(self.api.pages['m']))
        self.perform({'action': 'merge', 'source': self.entry('AI Agents')['id'], 'target': self.entry('RL')['id']}, key='merge')
        self.assertEqual(tag_notion.page_tags(self.api.pages['m']), ['RL', 'Data', 'Tool', 'Legacy', 'Infra'])
        self.assertEqual(tag_notion.page_tags(self.api.pages['t']), ['RL', 'Research'])
        for pid, page in self.api.pages.items():
            self.assertEqual(page['children'], before[pid]['children'])
            self.assertEqual(page['properties']['阅读状态'], before[pid]['properties']['阅读状态'])
        self.assertEqual(tag_store.normalize(['Agent', '智能体']), ['RL'])
        self.assertEqual(len(self.db.data['tag_versions/1']['entries']), 24)

    def test_single_delta_and_conflict_reread(self):
        self.api.add('m', 'materials', ['Agent', 'RL', 'Infra'])
        self.api.add('t', 'thoughts', ['Agent'])
        self.perform({'action': 'single', 'page_id': 'm', 'base': ['Agent', 'RL'], 'selected': ['RL', 'Data'],
                      'selection_ids': {e['name']: e['id'] for e in tag_store.load()['entries']}})
        self.assertEqual(tag_notion.page_tags(self.api.pages['m']), ['RL', 'Infra', 'Data'])
        self.assertEqual(tag_notion.page_tags(self.api.pages['t']), ['Agent'])
        called = 0
        real_update = tag_notion.notion.update_properties
        def conflict(pid, props):
            nonlocal called
            called += 1
            if called == 1:
                self.api.pages['m']['properties']['主题']['multi_select'].append({'name': 'Research'})
                raise RuntimeError('409 conflict')
            return real_update(pid, props)
        with patch.object(tag_notion.notion, 'update_properties', conflict):
            tag_notion.patch_tags('m', lambda ns: ['Stats' if n == 'Data' else n for n in ns])
        self.assertEqual(tag_notion.page_tags(self.api.pages['m']), ['RL', 'Infra', 'Stats', 'Research'])

    def test_stale_version_and_duplicate_confirmation(self):
        jid = self.new({'action': 'toggle', 'source': self.entry('Agent')['id']})
        tag_jobs.process(jid)
        self.assertTrue(tag_store.confirm(jid))
        self.assertFalse(tag_store.confirm(jid))
        self.perform({'action': 'edit', 'source': self.entry('RL')['id'], 'scope': '新范围', 'aliases': []}, key='edit')
        with self.assertRaises(ValueError):
            tag_jobs.process(jid)
        self.assertEqual(tag_store.get_job(jid)['status'], 'failed')

    def test_prompt_reply_is_control_and_cancelled_reply_not_saved(self):
        tag_ui.open_menu(self.msg())
        state = self.db.data['ui/tags_1_501']
        nonce = state['nonce']
        tag_ui.handle_callback(self.cb(f'tg:{nonce}:add', user=9))
        self.assertEqual(len(self.sent), 1)
        tag_ui.handle_callback(self.cb(f'tg:{nonce}:add'))
        prompt = 502
        self.assertFalse(tag_ui.handle_reply(self.msg(mid=43, text='正常想法')))
        self.assertTrue(tag_ui.handle_reply(self.msg(mid=44, reply=prompt, text='Robotics\n机器人实验')))
        jobid = self.queued[-1][0]
        self.assertEqual(tag_store.get_job(jobid)['change']['name'], 'Robotics')
        self.assertTrue(tag_ui.handle_reply(self.msg(mid=45, reply=prompt, text='不应存入想法')))
        # A delivered callback repeated with the original nonce cannot enqueue twice.
        tag_ui.handle_callback(self.cb(f'tg:{nonce}:add'))
        self.assertEqual(len(self.queued), 1)

    def test_reply_mapping_two_targets_and_unstored(self):
        self.api.add('m', 'materials', ['Agent'])
        self.api.add('t', 'thoughts', ['RL'])
        self.db.data['records/1:10'] = {'kind': 'material', 'notion_page_id': 'm', 'link_thought': {'id': 't'}}
        tag_ui.open_menu(self.msg(reply=10))
        state = self.db.data['ui/tags_1_501']
        self.assertEqual(state['mode'], 'targets')
        self.assertEqual(len(state['targets']), 2)
        tag_ui.open_menu(self.msg(mid=43, reply=99))
        self.assertIn('尚未入库', self.edits[-1][0][2])

    def test_callbacks_answer_first_and_no_inline_scan(self):
        tag_ui.open_menu(self.msg())
        state = self.db.data['ui/tags_1_501']
        tag_ui.handle_callback(self.cb(f"tg:{state['nonce']}:view:{self.entry('Agent')['id']}"))
        state = self.db.data['ui/tags_1_501']
        with patch.object(tag_notion, 'count', side_effect=AssertionError('webhook scan')):
            tag_ui.handle_callback(self.cb(f"tg:{state['nonce']}:toggle", key='toggle'))
        self.assertTrue(self.answers)
        self.assertTrue(self.queued)

    def test_tag_prompts_do_not_force_reply_or_link_bot_messages(self):
        tag_ui.open_menu(self.msg())
        state = self.db.data['ui/tags_1_501']
        tag_ui.handle_callback(self.cb(f"tg:{state['nonce']}:add"))
        self.assertIn('手动选择', self.sent[-1][0][1])
        self.assertFalse(tag_ui.handle_reply(self.msg(mid=43, text='正常想法')))
        self.assertTrue(tag_ui.handle_reply(self.msg(mid=44, reply=502, text='格式不对')))
        for args, kwargs in self.sent:
            self.assertNotIn('reply_to', kwargs)
            self.assertNotIn('force_reply', kwargs.get('markup') or {})
        self.assertTrue(tag_ui.handle_reply(self.msg(mid=45, reply=502, text='Robotics\n机器人实验')))
        self.assertEqual(len(self.queued), 1)

    def test_expired_menu_refreshes_without_running_mutation(self):
        tag_ui.open_menu(self.msg())
        state = self.db.data['ui/tags_1_501']
        state['expires'] = 0
        tag_ui.handle_callback(self.cb(f"tg:{state['nonce']}:toggle"))
        self.assertEqual(self.db.data['ui/tags_1_501']['mode'], 'list')
        self.assertGreater(self.db.data['ui/tags_1_501']['expires'], 0)
        self.assertIn('已刷新', self.edits[-1][0][2])
        self.assertFalse(self.queued)
        current = self.db.data['ui/tags_1_501']
        tag_ui.handle_callback(self.cb(f"tg:{current['nonce']}:page:1", key='fresh'))
        self.assertEqual(self.db.data['ui/tags_1_501']['page'], 1)

    def test_old_close_button_works_after_expiry_and_nonce_change(self):
        tag_ui.open_menu(self.msg())
        state = self.db.data['ui/tags_1_501']
        old_nonce = state['nonce']
        state.update(expires=0, nonce='changed')
        tag_ui.handle_callback(self.cb(f'tg:{old_nonce}:close', user=9))
        self.assertFalse(state['closed'])
        tag_ui.handle_callback(self.cb(f'tg:{old_nonce}:close'))
        self.assertTrue(self.db.data['ui/tags_1_501']['closed'])
        self.assertEqual(self.edits[-1][1]['markup']['inline_keyboard'], [])

    def test_new_close_button_and_closed_menu_stay_closed(self):
        tag_ui.open_menu(self.msg())
        state = self.db.data['ui/tags_1_501']
        old_nonce = state['nonce']
        self.assertEqual(self.edits[-1][1]['markup']['inline_keyboard'][-1][0]['callback_data'], 'tg:close')
        tag_ui.handle_callback(self.cb('tg:close'))
        tag_ui.handle_callback(self.cb(f'tg:{old_nonce}:back', key='old'))
        self.assertTrue(self.db.data['ui/tags_1_501']['closed'])
        self.assertEqual(self.edits[-1][1]['markup']['inline_keyboard'], [])

    def test_failed_render_recovers_visible_keyboard_and_can_close(self):
        tag_ui.open_menu(self.msg())
        old = self.db.data['ui/tags_1_501']['nonce']
        with patch.object(tag_ui.telegram, 'edit', side_effect=RuntimeError('Telegram timeout')):
            with self.assertRaises(RuntimeError):
                tag_ui.handle_callback(self.cb(f'tg:{old}:page:1'))
        self.assertEqual(self.db.data['ui/tags_1_501']['busy_until'], 0)
        tag_ui.handle_callback(self.cb(f'tg:{old}:view:{self.entry("Agent")["id"]}', key='retry'))
        self.assertIn('已刷新', self.edits[-1][0][2])
        fresh = self.db.data['ui/tags_1_501']['nonce']
        tag_ui.handle_callback(self.cb(f'tg:{fresh}:view:{self.entry("Agent")["id"]}', key='detail'))
        self.assertEqual(self.db.data['ui/tags_1_501']['mode'], 'detail')
        tag_ui.handle_callback(self.cb('tg:close', key='close'))
        self.assertTrue(self.db.data['ui/tags_1_501']['closed'])

    def test_duplicate_or_busy_callback_does_not_refresh_or_enqueue(self):
        tag_ui.open_menu(self.msg())
        old = self.db.data['ui/tags_1_501']['nonce']
        tag_ui.handle_callback(self.cb(f'tg:{old}:page:1'))
        count = len(self.edits)
        tag_ui.handle_callback(self.cb(f'tg:{old}:page:1'))
        self.assertEqual(len(self.edits), count)
        self.db.data['ui/tags_1_501']['busy_until'] = float('inf')
        current = self.db.data['ui/tags_1_501']['nonce']
        tag_ui.handle_callback(self.cb(f'tg:{current}:toggle', key='busy'))
        self.assertEqual(len(self.edits), count)
        self.assertFalse(self.queued)

    def test_navigation_filter_only(self):
        filt = {'and': [{'property': 'tag-id', 'multi_select': {'contains': 'Agent'}},
                        {'property': '状态', 'select': {'equals': 'Agent'}}]}
        new = tag_notion.replace_filter(filt, 'Agent', 'AI Agents', ('tag-id',))
        self.assertEqual(new['and'][0]['multi_select']['contains'], 'AI Agents')
        self.assertEqual(new['and'][1], filt['and'][1])
        self.assertEqual(filt['and'][0]['multi_select']['contains'], 'Agent')

    def test_navigation_fallback_is_not_claimed_complete(self):
        with patch.object(tag_notion, 'discover_navigation', return_value={'views': [], 'pages': [], 'manual': ['具体导航页收尾']}):
            jid = self.new({'action': 'rename', 'source': self.entry('Agent')['id'], 'name': 'AI Agents'})
            tag_jobs.process(jid)
            self.assertIn('导航收尾', self.edits[-1][0][2])
            tag_store.confirm(jid)
            tag_jobs.process(jid)
        self.assertEqual(tag_store.get_job(jid)['status'], 'needs_navigation')
        self.assertEqual(tag_store.load()['version'], 2)
        tag_ui.handle_callback(self.cb('tg:navdone:' + jid))
        self.assertEqual(tag_store.get_job(jid)['status'], 'done')

    def test_material_image_pdf_and_chat_receive_current_definitions(self):
        import json
        from app import chat_share
        self.perform({'action': 'add', 'id': 'robotics', 'name': 'Robotics', 'scope': '测试具身机器人定义', 'aliases': ['机器人']})
        captured = []
        def material(messages, *args, **kwargs):
            captured.append(messages)
            return json.dumps({'title': 'test', 'overview': '中文概览', 'material_type': '网页',
                               'topics': ['机器人', 'unknown'], 'abstract_zh': '译文',
                               'topic_evidence': [{'tag': '机器人', 'basis': '主要讨论机器人', 'quote': '测试具身机器人定义'}],
                               'tag_review': {'needed': False, 'reason': '', 'suggestions': []}})
        with patch.object(enrich.settings, 'active', return_value={'base_url': 'https://mock', 'model': 'mock'}), patch.object(enrich, '_chat', material):
            for kwargs in ({}, {'images': [(b'mock', 'image/png')]}, {'paper_abstract': 'abstract'}):
                result, error = enrich.enrich('来源标题', '测试具身机器人定义', **kwargs)
                self.assertEqual(error, '')
                self.assertEqual(result.topics, ['Robotics'])
                self.assertIn('Robotics：测试具身机器人定义', captured[-1][0]['content'])
        conversation = chat_share.Conversation('ChatGPT', 'https://test', 'test', [{'id': 1, 'role': 'user', 'text': '测试具身机器人定义'}, {'id': 2, 'role': 'assistant', 'text': 'answer'}])
        def chat(messages, *args, **kwargs):
            captured.append(messages)
            data = {s: [] for s in chat_notes.SECTIONS}
            data.update(title='测试', topics=['机器人', 'unknown'], questions=[{'text': '问题', 'sources': [1]}], conclusions=[{'text': '结论', 'sources': [2]}])
            data.update(topic_evidence=[{'tag': '机器人', 'basis': '讨论机器人范围', 'quote': '测试具身机器人定义'}],
                        tag_review={'needed': False, 'reason': '', 'suggestions': []})
            return json.dumps(data)
        with patch.object(enrich, '_chat', chat):
            result = chat_notes.digest(conversation)
            self.assertEqual(result['topics'], ['Robotics'])
            self.assertIn('Robotics：测试具身机器人定义', captured[-1][0]['content'])

    def test_duplicate_menu_delivery_does_not_send_second_menu(self):
        tag_ui.open_menu(self.msg())
        tag_ui.open_menu(self.msg())
        self.assertEqual(len(self.sent), 1)

    def test_write_lease_and_latest_commit_validation(self):
        source = self.entry('Agent')['id']
        self.perform({'action': 'toggle', 'source': source})
        # A stale model result cannot write back a disabled tag.
        result = tag_notion.notion._create_tagged_page({'properties': {'主题': {'multi_select': [{'name': 'Agent'}, {'name': 'RL'}, {'name': 'unknown'}]}}})
        self.assertEqual(tag_notion.page_tags(result), ['RL'])
        with tag_store.write_guard():
            with self.assertRaises(RuntimeError):
                with tag_store.write_guard():
                    pass


if __name__ == '__main__':
    unittest.main()
