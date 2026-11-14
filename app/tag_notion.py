"""Property-only tag edits and independently versioned Notion Views API."""
import copy
from urllib.parse import urlencode, unquote

import httpx

from . import config, notion, tag_store

VIEW_VERSION = '2026-03-11'


def page_tags(page):
    return [t['name'] for t in page.get('properties', {}).get('主题', {}).get('multi_select', [])]


def patch_tags(page_id, transform):
    """Always re-read on retry; send only 主题, preserving body and other fields."""
    with tag_store.write_guard():
        for attempt in range(3):
            page = notion._call('GET', '/pages/' + page_id)
            if page.get('archived') or page.get('in_trash'):
                raise ValueError('页面已删除，无法编辑标签')
            old = page_tags(page)
            new = list(dict.fromkeys(transform(old)))
            if new == old:
                return False
            try:
                notion.update_properties(page_id, {'主题': {'multi_select': [{'name': n} for n in new]}})
                return True
            except RuntimeError as e:
                if '409' not in str(e) or attempt == 2:
                    raise
    return False


def selected_delta(old, base, chosen):
    """Apply only the user's additions/removals, keeping concurrent new labels."""
    removed = set(base) - set(chosen)
    return list(dict.fromkeys([n for n in old if n not in removed] + [n for n in chosen if n not in base]))


def databases():
    ids = [config.NOTION_MATERIAL_DB, config.NOTION_THOUGHT_DB]
    if not all(ids) or len(set(ids)) != 2:
        raise ValueError('需要配置两个不同的 Notion 数据库')
    return ids


def query(dbid, source, limit=50, cursor=None):
    body = {'page_size': limit, 'filter': {'property': '主题', 'multi_select': {'contains': source}}}
    if cursor:
        body['start_cursor'] = cursor
    return notion._call('POST', '/databases/' + dbid + '/query', body)


def count(dbid, source):
    total, cursor = 0, None
    while True:
        result = query(dbid, source, cursor=cursor)
        total += len(result.get('results', []))
        if not result.get('has_more'):
            return total
        cursor = result['next_cursor']


def check_option_spelling(entries):
    formal = {tag_store.topics.key(e['name']): e['name'] for e in entries}
    for db in databases():
        schema = notion._call('GET', '/databases/' + db)
        for option in schema['properties']['主题']['multi_select']['options']:
            canonical = formal.get(tag_store.topics.key(option['name']))
            if canonical and canonical != option['name']:
                raise ValueError(f"Notion 已有同名选项「{option['name']}」，请使用该写法补充定义；显示大小写需在 Notion 界面统一")


def sync_options(dbid, entries, remove=None):
    schema = notion._call('GET', '/databases/' + dbid)
    prop = schema['properties']['主题']
    options = [{k: o[k] for k in ('id', 'name', 'color') if k in o}
               for o in prop['multi_select']['options'] if o['name'] != remove]
    existing = {tag_store.topics.key(o['name']): o for o in options}
    for entry in entries:
        key = tag_store.topics.key(entry['name'])
        if key in existing:
            if existing[key]['name'] != entry['name']:
                raise ValueError(f"Notion 已有同名选项「{existing[key]['name']}」，请使用该写法补充定义；修改显示大小写需在 Notion 界面完成")
        else:
            option = {'name': entry['name']}
            options.append(option)
            existing[key] = option
    notion._call('PATCH', '/databases/' + dbid,
                 {'properties': {'主题': {'multi_select': {'options': options}}}})
    notion._topics_cache.clear()


def view_call(method, path, payload=None):
    headers = notion._headers()
    headers['Notion-Version'] = VIEW_VERSION
    r = httpx.request(method, notion.API + path, headers=headers, json=payload, timeout=40)
    if r.status_code >= 400:
        raise RuntimeError(f'Notion 视图接口 HTTP {r.status_code}: {r.text[:200]}')
    return r.json()


def replace_filter(value, old, new, property_ids=()):
    """Change only literal tag conditions; keep every other filter intact."""
    result = copy.deepcopy(value)
    if isinstance(result, dict):
        if result.get('property') in ('主题', *property_ids):
            for key in ('contains', 'does_not_contain'):
                multi = result.get('multi_select', {})
                if multi.get(key) == old:
                    multi[key] = new
        for k, v in result.items():
            if isinstance(v, (dict, list)):
                result[k] = replace_filter(v, old, new, property_ids)
    elif isinstance(result, list):
        result = [replace_filter(v, old, new, property_ids) for v in result]
    return result


def containing_page(parent):
    for _ in range(20):
        if parent.get('page_id'):
            return parent['page_id']
        if not parent.get('block_id'):
            return None
        parent = notion._call('GET', '/blocks/' + parent['block_id']).get('parent', {})
    raise ValueError('导航父页面层级异常')


def navigation_registry():
    snap = tag_store.store.db().collection('settings').document('tag_navigation').get()
    return (snap.to_dict() or {}).get('pages', []) if snap.exists else []


def discover_navigation(old, new):
    """Enumerate all accessible linked views by data source, without API upgrade.

    Registry adds known navigation pages and provides concrete UI fallback
    items if the separately versioned Views endpoint is unavailable.
    """
    pages = [p for p in navigation_registry() if p['name'] == old]
    plan = {'views': [], 'pages': pages, 'manual': []}
    try:
        seen = set()
        for dbid in databases():
            db = view_call('GET', '/databases/' + dbid)
            property_id = unquote(notion._call('GET', '/databases/' + dbid)['properties']['主题']['id'])
            sources = db.get('data_sources', [])
            if not sources:
                raise RuntimeError('视图 API 未返回 data_source_id')
            for source in sources:
                cursor = None
                while True:
                    params = {'data_source_id': source['id'], 'page_size': 100}
                    if cursor:
                        params['start_cursor'] = cursor
                    listing = view_call('GET', '/views?' + urlencode(params))
                    for v in listing.get('results', []):
                        if v['id'] in seen:
                            continue
                        seen.add(v['id'])
                        view = view_call('GET', '/views/' + v['id'])
                        changed = replace_filter(view.get('filter'), old, new, (property_id,))
                        if changed != view.get('filter') or view.get('name') == old:
                            plan['views'].append({'id': view['id'], 'name': view.get('name', ''),
                                                  'property_id': property_id})
                            container = (view.get('parent') or {}).get('database_id')
                            if container:
                                linked = view_call('GET', '/databases/' + container)
                                pid = containing_page(linked.get('parent') or {})
                                if pid and pid not in {p['id'] for p in plan['pages']}:
                                    page = notion._call('GET', '/pages/' + pid)
                                    title = ''.join(t.get('plain_text', t.get('text', {}).get('content', ''))
                                                    for prop in page.get('properties', {}).values()
                                                    for t in prop.get('title', []))
                                    if title == old:
                                        plan['pages'].append({'id': pid, 'name': old})
                    if not listing.get('has_more'):
                        break
                    cursor = listing['next_cursor']
    except Exception as e:
        plan['manual'].append(f'视图接口不可用：{e}')
        for p in pages:
            plan['manual'].append(f"{old} → {new}：https://www.notion.so/{p['id'].replace('-', '')}；修改全部资料、待读、相关想法三个视图的主题筛选")
        if not pages:
            plan['manual'].append(f'在「全部标签」和快捷导航检查 {old} 的名称及主题筛选，统一为 {new}；当前连接未能枚举视图')
    return plan


def sync_navigation(plan, old, new):
    for item in plan['views']:
        # Re-read so other layout/filter edits survive the migration.
        view = view_call('GET', '/views/' + item['id'])
        payload = {}
        filt = replace_filter(view.get('filter'), old, new, (item['property_id'],))
        if filt != view.get('filter'):
            payload['filter'] = filt
        if view.get('name') == old:
            payload['name'] = new
        if payload:
            view_call('PATCH', '/views/' + item['id'], payload)
    for item in plan['pages']:
        page = notion._call('GET', '/pages/' + item['id'])
        for name, prop in page.get('properties', {}).items():
            if prop.get('type') == 'title':
                title = ''.join(t.get('plain_text', t.get('text', {}).get('content', '')) for t in prop.get('title', []))
                if title == old:
                    notion.update_properties(item['id'], {name: notion._title(new)})
                break
    if not plan['manual']:
        pages = navigation_registry()
        for p in pages:
            if p['name'] == old:
                p['name'] = new
        tag_store.store.db().collection('settings').document('tag_navigation').set({'pages': pages})
