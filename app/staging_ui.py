"""Read-only Telegram report of the current chat's unresolved content."""
import datetime
import re

from . import config, pipeline, store, telegram

PAGE_SIZE = 4


def clip(value, limit):
    text = ' '.join(str(value or '').split())
    encoded = text.encode('utf-16-le')
    return text if len(encoded) <= limit * 2 else encoded[:(limit - 1) * 2].decode('utf-16-le', errors='ignore') + '…'


def reason(value):
    text = telegram.redact(str(value or ''))
    for key in (config.LLM_API_KEY, config.NOTION_TOKEN, config.TELEGRAM_BOT_TOKEN):
        if key:
            text = text.replace(key, '<已隐藏>')
    text = re.sub(r'\b(?:sk-|ntn_|secret_)[A-Za-z0-9_-]{8,}', '<已隐藏>', text)
    return clip(text, 200)


def message(row):
    payload = row.get('payload') or {}
    return row.get('latest_message') or payload.get('message') or payload.get('edited_message') or {}


def timestamp(value):
    try:
        return datetime.datetime.fromtimestamp(float(value), datetime.timezone.utc).strftime('%m-%d %H:%M UTC')
    except (ValueError, TypeError, OverflowError, OSError):
        return '未知时间'


def report(chat_id, args=()):
    if len(args) > 1 or (args and (not args[0].isascii() or not args[0].isdigit() or len(args[0]) > 6 or int(args[0]) < 1)):
        return '用法：/staged 查看第一页；/staged 2 查看第二页。'
    page = int(args[0]) if args else 1
    rows = []
    staged = [r for r in store.staged_urls() if r.get('chat_id') == chat_id]
    staged_urls = {store.url_hash(r['url']) for r in staged if r.get('url')}
    seen_messages = {(r.get('chat_id'), r.get('message_id')) for r in staged}
    for row in staged:
        kind = '想法关联未完成' if row.get('page_id') else ('聊天提炼暂存' if row.get('ingest_kind') == 'chat_share' else '链接暂存')
        rows.append({'kind': kind, 'url': row.get('url'), 'reason': row.get('error') or '未记录失败原因',
                     'attempts': row.get('attempts', 0), 'at': row.get('last_at') or row.get('first_at'),
                     'message': row.get('message_id')})
    counts = [len(rows)]
    for getter, kind in ((store.waiting_for_llm, '等待 LLM'), (store.failed_updates, '处理失败')):
        before = len(rows)
        for row in getter():
            msg = message(row)
            if (msg.get('chat') or {}).get('id') != chat_id:
                continue
            text = msg.get('text') or msg.get('caption') or ''
            if text.strip().startswith('/'):
                continue  # Command failures are not staged content.
            url = pipeline.first_url(msg)
            key = (chat_id, msg.get('message_id'))
            if key in seen_messages or (url and store.url_hash(url) in staged_urls):
                continue
            seen_messages.add(key)
            document = msg.get('document') or {}
            label = document.get('file_name') or text or ('图片' if msg.get('photo') else '消息')
            rows.append({'kind': kind, 'url': url, 'label': label,
                         'reason': row.get('error') or ('LLM 请求未完成，每 15 分钟自动检查重试' if kind == '等待 LLM' else '未记录失败原因'),
                         'attempts': row.get('attempts', 0),
                         'at': row.get('updated_at') or row.get('parked_at') or row.get('received_at'),
                         'message': msg.get('message_id')})
        counts.append(len(rows) - before)
    if not rows:
        return '📥 暂存区为空，也没有等待 LLM 或处理失败的消息。\n已入库材料的「需处理」状态可在 Notion 资料库查看。'
    rows.sort(key=lambda r: r.get('at') or 0, reverse=True)
    pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    page = min(page, pages)
    lines = [f'📥 暂存与失败：共 {len(rows)} 条（第 {page}/{pages} 页）',
             f'暂存 {counts[0]} · 等待 LLM {counts[1]} · 处理失败 {counts[2]}']
    for i, row in enumerate(rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE], (page - 1) * PAGE_SIZE + 1):
        lines += ['', f"{i}. {row['kind']}"]
        if row.get('label'):
            lines.append(clip(row['label'], 120))
        if row.get('url'):
            lines.append(clip(row['url'], 400))
        lines += ['原因：' + reason(row['reason']),
                  f"尝试 {row['attempts']} 次 · {timestamp(row['at'])} · 原消息 #{row.get('message') or '?'}"]
    if page < pages:
        lines += ['', f'下一页：/staged {page + 1}']
    lines += ['', '暂存链接：/retry 重试；等待 LLM：每 15 分钟自动检查重试。',
              '已入库「需处理」材料请在 Notion 查看。']
    return '\n'.join(lines)
