"""Relationships authorized by a Telegram reply, including either arrival order."""
from . import notion, store


def ids(value):
    # Also usable with the simple in-memory record stubs in offline tests.
    if isinstance(value, store.firestore.ArrayUnion):
        return list(value.values)
    return list(value or [])


def reply_id(msg):
    reply = msg.get('reply_to_message') or {}
    if reply.get('message_id'):
        return reply['message_id']
    own = store.get_record(msg['chat']['id'], msg['message_id']) or {}
    return own.get('explicit_reply_message_id')


def remember_reply(msg):
    reply = msg.get('reply_to_message') or {}
    if reply.get('message_id'):
        store.put_record(msg['chat']['id'], msg['message_id'],
                         explicit_reply_message_id=reply['message_id'])


def parent_record(msg):
    mid = reply_id(msg)
    if not mid:
        return {}
    return store.get_record(msg['chat']['id'], mid) or {}


def note_ref(record):
    if record.get('link_thought'):
        return record['link_thought']['id']
    return record.get('notion_page_id') if record.get('kind') == 'thought' else None


def note_targets(msg):
    parent = parent_record(msg)
    targets = [parent['notion_page_id']] if parent.get('kind') == 'material' and parent.get('notion_page_id') else []
    own = store.get_record(msg['chat']['id'], msg['message_id']) or {}
    return list(dict.fromkeys(targets + ids(own.get('explicit_reply_materials'))))


def relate(note_id, material_ids):
    page = notion.get_page(note_id)
    if not page:
        raise RuntimeError('明确引用的笔记暂时不可读取')
    existing = [r['id'] for r in page.get('properties', {}).get('关联资料', {}).get('relation', [])]
    notion.link_thought_to_material(note_id, list(dict.fromkeys(existing + material_ids)))


def queue_note(msg, note_id, linked=()):
    """A text replying to a link can arrive before that material is ready."""
    mid = reply_id(msg)
    if not mid:
        return
    parent = parent_record(msg)
    if parent.get('kind') == 'thought':
        return  # A note replying to a note does not imply a material target.
    if parent.get('kind') == 'material' and parent.get('notion_page_id'):
        if parent['notion_page_id'] not in linked:
            relate(note_id, [parent['notion_page_id']])
        return
    # The reply itself is the authorization; persist it on the target record.
    store.put_record(msg['chat']['id'], mid,
                     explicit_reply_notes=store.firestore.ArrayUnion([note_id]))


def bind_material(msg, material_id, url=''):
    """Link replying to text, or completion of text replying to a waiting link."""
    own = store.get_record(msg['chat']['id'], msg['message_id']) or {}
    notes = ids(own.get('explicit_reply_notes'))
    mid = reply_id(msg)
    if mid:
        parent = parent_record(msg)
        note = note_ref(parent)
        if note:
            notes.append(note)
        elif parent.get('kind') != 'material':
            # The original text task may not have run yet. Its later note
            # creation will read this explicit target from its own record.
            store.put_record(msg['chat']['id'], mid,
                             explicit_reply_materials=store.firestore.ArrayUnion([material_id]))
    pending = []
    for note_id in dict.fromkeys(notes):
        try:
            relate(note_id, [material_id])
        except Exception:
            pending.append(note_id)
    if pending and url:
        store.stage_url(url, msg['chat']['id'], msg['message_id'], '明确引用的笔记关联尚未完成')
        store.update_chat_stage(url, page_id=material_id,
                                explicit_thought_ids=store.firestore.ArrayUnion(pending))
    elif pending:
        raise RuntimeError('明确引用的笔记关联尚未完成')
