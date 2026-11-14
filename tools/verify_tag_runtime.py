"""Opt-in live drill in NEW Notion databases and an isolated Firestore namespace.

No production page/schema/tag is edited. The temporary Notion root is archived
on success; the isolated Firestore state is retained for audit and excluded from
production backups as restore_drills. Use --local-gcloud --project PROJECT.
"""
import argparse
import copy
import json
import os
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import backup, config, notion, store, tag_jobs, tag_notion, tag_store, topics


class Namespace:
    def __init__(self, client, run):
        self.client, self.root = client, client.collection('restore_drills').document(run)
    def collection(self, name):
        return self.root.collection(name)
    def transaction(self):
        return self.client.transaction()


def stable(value):
    # Signed download query strings and expiry timestamps change on a read.
    from urllib.parse import urlsplit, urlunsplit
    if isinstance(value, dict):
        result = {k: stable(v) for k, v in value.items() if k not in ('request_id', 'expiry_time')}
        if value.get('type') == 'file' and isinstance(result.get('file'), dict):
            url = result['file'].get('url')
            if url:
                u = urlsplit(url)
                result['file']['url'] = urlunsplit((u.scheme, u.netloc, u.path, '', ''))
        return result
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local-gcloud', action='store_true')
    parser.add_argument('--project', required=True)
    parser.add_argument('--report', default='/tmp/kb-tag-runtime-drill.json')
    args = parser.parse_args()
    _, fs, _ = backup.clients(args.local_gcloud, args.project)
    production = [config.NOTION_MATERIAL_DB, config.NOTION_THOUGHT_DB]
    before = {d: stable(notion.query_database(d)) for d in production}
    schema = notion._call('GET', '/databases/' + production[0])
    parent = tag_notion.containing_page(schema['parent'])
    if not parent:
        raise ValueError('找不到隔离页面可用的父页面')
    run = 'tag-validation-' + uuid.uuid4().hex[:12]
    report = {'run': run, 'checks': [], 'production_databases': production}
    root = notion._call('POST', '/pages', {'parent': {'page_id': parent},
                                         'properties': {'title': notion._title('标签管理隔离验证 ' + run)}})
    report['root_id'] = root['id']
    dbs = []
    try:
        for name in ('标签测试资料', '标签测试笔记'):
            db = notion._call('POST', '/databases', {'parent': {'page_id': root['id']},
                'title': notion._rt(name), 'properties': {'标题': {'title': {}},
                '主题': {'multi_select': {'options': [{'name': n} for n in topics.TOPICS]}},
                '阅读状态': {'select': {'options': [{'name': '已读'}]}}}})
            dbs.append(db['id'])
        config.NOTION_MATERIAL_DB, config.NOTION_THOUGHT_DB = dbs
        store._db = Namespace(fs, run)
        tag_store.invalidate()
        tag_store.initialize()
        pages = []
        for db in dbs:
            pages.append(notion._call('POST', '/pages', {'parent': {'database_id': db},
                'properties': {'标题': notion._title('隔离测试'), '主题': notion._multi(['Agent', 'RL', 'Research']),
                    '阅读状态': notion._select('已读')}, 'children': notion.paragraphs('仅用于标签验证的正文，不能改写。')}))
        page_before = [notion._call('GET', '/pages/' + p['id']) for p in pages]
        body_before = [notion.list_children(p['id']) for p in pages]
        # One NEW navigation page and NEW linked filtered view for both sources.
        nav = notion._call('POST', '/pages', {'parent': {'page_id': root['id']},
                                            'properties': {'title': notion._title('Agent')}})
        store.db().collection('settings').document('tag_navigation').set({'pages': [{'id': nav['id'], 'name': 'Agent'}]})
        views = []
        for db in dbs:
            source = tag_notion.view_call('GET', '/databases/' + db)['data_sources'][0]['id']
            views.append(tag_notion.view_call('POST', '/views', {'create_database': {'parent': {'type': 'page_id', 'page_id': nav['id']}},
                'data_source_id': source, 'name': 'Agent', 'type': 'list',
                'filter': {'and': [{'property': '主题', 'multi_select': {'contains': 'Agent'}},
                                   {'property': '阅读状态', 'select': {'equals': '已读'}}]}}))
        def entry(name):
            return next(e for e in tag_store.load()['entries'] if e['name'] == name)
        def perform(change, label):
            jid = tag_store.new_job(run + ':' + label, change, 0, 0, 0)
            tag_jobs.process(jid)
            assert tag_store.get_job(jid)['status'] == 'ready'
            assert tag_store.confirm(jid)
            tag_jobs.process(jid)
            job = tag_store.get_job(jid)
            assert job['status'] == 'done', job
            report['checks'].append(label)
            print('live isolated drill:', label, 'passed', flush=True)
            return job
        with patch.object(tag_jobs, 'notify', lambda *a: None):
            perform({'action': 'add', 'id': 'drill', 'name': 'Drill', 'scope': '隔离测试范围', 'aliases': ['测试词']}, 'add')
            eid = entry('Agent')['id']
            perform({'action': 'rename', 'source': eid, 'name': 'AI Agents'}, 'rename')
            assert tag_store.normalize(['Agent', '智能体']) == ['AI Agents']
            for v in views:
                current = tag_notion.view_call('GET', '/views/' + v['id'])
                assert 'AI Agents' in json.dumps(current['filter'])
                assert '已读' in json.dumps(current['filter'], ensure_ascii=False)
                assert current['name'] == 'AI Agents'
            report['checks'].append('navigation_filters_and_names')
            perform({'action': 'merge', 'source': eid, 'target': entry('RL')['id']}, 'merge')
            for p in pages:
                assert tag_notion.page_tags(notion._call('GET', '/pages/' + p['id'])) == ['RL', 'Research']
            rid = entry('RL')['id']
            perform({'action': 'toggle', 'source': rid}, 'disable')
            assert tag_store.normalize(['RL'], writing=True) == []
            perform({'action': 'toggle', 'source': rid}, 'restore')
            assert tag_store.normalize(['RL']) == ['RL']
            perform({'action': 'single', 'page_id': pages[0]['id'], 'base': ['RL', 'Research'], 'selected': ['Research'],
                     'selection_ids': {e['name']: e['id'] for e in tag_store.load()['entries']}}, 'single')
            assert tag_notion.page_tags(notion._call('GET', '/pages/' + pages[1]['id'])) == ['RL', 'Research']
        for i, p in enumerate(pages):
            current = notion._call('GET', '/pages/' + p['id'])
            assert current['properties']['阅读状态'] == page_before[i]['properties']['阅读状态']
            assert notion.list_children(p['id']) == body_before[i]
        report['checks'].append('bodies_and_reading_preserved')
        for db in production:
            assert stable(notion.query_database(db)) == before[db], 'Production page state changed during drill'
        report['checks'].append('production_pages_unchanged')
        report['status'] = 'passed'
    except Exception as e:
        report.update(status='failed', error=str(e))
        raise
    finally:
        # Keep the live failure available for debugging; successful tests are hidden.
        if report.get('status') == 'passed':
            notion._call('PATCH', '/pages/' + root['id'], {'archived': True})
            report['root_archived'] = True
        with open(args.report, 'w') as f:
            os.chmod(args.report, 0o600)
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
