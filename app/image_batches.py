"""Bounded image reading with durable artifacts, continuation and one final page."""
import copy
import hashlib
import json
import logging
import re
import time

import httpx
from . import config, enrich, extract, storage, store, tasks, work_budget

log = logging.getLogger(__name__)
VERSION = 1
BATCH_IMAGES = 4
MAX_NO_PROGRESS_ATTEMPTS = 3
# The current NRP gateway returns an empty HTTP 500 for ~5 MB bodies. Keep
# base64 image data plus prompt reserve below the independently verified size.
MAX_REQUEST_BYTES = 3 * 1024 * 1024


def request_size(images):
    return sum((len(blob)+2)//3*4 for blob, _ in images) + 100_000


class Pending(RuntimeError):
    pass


class Busy(Pending):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode()


def put_json(url, kind, value):
    data = encoded(value)
    sha = hashlib.sha256(data).hexdigest()
    uri = storage.archive(data, f'images-{store.url_hash(url)}-{kind}-{sha[:20]}.json', 'application/json; charset=utf-8')
    if not uri:
        raise Pending('分批结果归档失败，保留已完成进度')
    return uri


def get_json(uri):
    data = storage.read_archive(uri)
    expected = re.search(r'-([a-f0-9]{20})\.json$', uri)
    if expected is None or hashlib.sha256(data).hexdigest()[:20] != expected.group(1):
        raise Pending('分批归档 SHA 校验失败，停止使用该检查点')
    return json.loads(data)


def checkpoint(job_url, job, **fields):
    store.update_image_job(job_url, **fields)
    job.update(fields)


def auto_paused(job):
    return job.get('failure_streak', 0) >= MAX_NO_PROGRESS_ATTEMPTS


def progress(job):
    return (len(job.get('assets', {})), job.get('completed', 0),
            len(job.get('merge_steps', {})), bool(job.get('summary_uri')),
            bool(job.get('page')), bool(job.get('body_verified')))


def record_failure(url, job, starting_progress, error):
    streak = 0 if progress(job) != starting_progress else job.get('failure_streak', 0) + 1
    if streak >= MAX_NO_PROGRESS_ATTEMPTS:
        error = f'已暂停自动重试，可用 /retry 手动重试。{error}'
    checkpoint(url, job, status='pending', error=error, failure_streak=streak)
    return error


def archive_body(url, text):
    sha = hashlib.sha256(text.encode()).hexdigest()
    return storage.archive_text(text, f'images-{store.url_hash(url)}-body-{sha[:20]}.txt')


def read_batch(title, body, indices, images):
    prompt = f'帖子标题：{title}\n帖子正文（仅作上下文）：{body[:4000]}\n本批局部图片编号：0–{len(images)-1}。只读取所给图片。'
    messages = [{'role': 'system', 'content': enrich.SYSTEM_IMAGES + '\n只输出 JSON {"images":[{"index":0,"informative":true,"summary":"中文逐图总结","text":"可辨认原文","note":"不确定性"}]}，不输出整帖总结。'},
                {'role': 'user', 'content': enrich._content_with_images(prompt, images)}]
    if len(encoded(messages)) > MAX_REQUEST_BYTES:
        raise Pending('当前批次请求过大，需要拆成更小批次')
    schema = copy.deepcopy(enrich.JSON_SCHEMA)
    schema['schema'] = {'type': 'object', 'properties': {'images': copy.deepcopy(enrich.JSON_SCHEMA['schema']['properties']['images'])}, 'required': ['images'], 'additionalProperties': False}
    schema['schema']['properties']['images'].update(minItems=len(images), maxItems=len(images))
    content = None
    for use_schema in ((True, False) if config.LLM_USE_SCHEMA else (False,)):
        try:
            content = enrich._chat(messages, use_schema, max_tokens=12000,
                                   json_schema=schema, request_timeout=config.IMAGE_LLM_TIMEOUT,
                                   disable_thinking=True)
            break
        except enrich._Rejected as exc:
            if exc.status >= 500 or exc.status == 200 or not use_schema:
                raise Pending('读图请求未完成：' + str(exc)) from None
        except httpx.HTTPError as exc:
            raise Pending('读图请求超时或网络异常：' + type(exc).__name__) from None
    try:
        data = enrich._extract_json(content)
        reads = [enrich.ImageRead.model_validate(r) for r in data['images']]
        if sorted(r.index for r in reads) != list(range(len(images))):
            raise ValueError('本批图片编号不完整或重复')
        reads.sort(key=lambda r: r.index)
        if any(r.informative and not r.summary.strip() for r in reads):
            raise ValueError('有信息图片缺少总结')
        for r, index in zip(reads, indices):
            r.index = index
        return reads
    except (ValueError, KeyError, TypeError) as exc:
        raise Pending('逐图结果校验失败：' + str(exc)) from None


def asset(url, job, index, image_url):
    saved = job.get('assets', {}).get(str(index))
    if saved:
        data = storage.read_archive(saved['vision_uri'])
        if hashlib.sha256(data).hexdigest() != saved['vision_sha']:
            raise Pending(f'图 {index+1} 的识别副本校验失败')
        return (data, saved['vision_mime'])
    # One image at a time: the historical per-post 9/12MB caps no longer truncate a post.
    images, missing = extract.download_images([image_url])
    if missing or len(images) != 1 or len(images.originals) != 1:
        raise Pending(f'图 {index+1} 下载、解码或单图容量检查未通过')
    _, original, mime = images.originals[0]
    vision, vision_mime = images[0]
    sha = hashlib.sha256(original).hexdigest()
    name = f'images-{store.url_hash(url)}-{index}-{sha[:20]}'
    original_uri = storage.archive(original, name + '.original', mime)
    vision_uri = storage.archive(vision, name + '.vision', vision_mime)
    if not original_uri or not vision_uri:
        raise Pending(f'图 {index+1} 原件或识别副本归档失败')
    info = {'index': index, 'sha': sha, 'original_uri': original_uri, 'mime': mime,
            'vision_uri': vision_uri, 'vision_mime': vision_mime,
            'vision_sha': hashlib.sha256(vision).hexdigest()}
    checkpoint(url, job, assets={**job.get('assets', {}), str(index): info})
    return vision, vision_mime


def read_all(url, job, art, say):
    reads = {}
    for step in job.get('steps', []):
        result = get_json(step['uri'])
        if [r.get('index') for r in result] != step['indices']:
            raise Pending('批次结果与来源图片编号不一致')
        expected = hashlib.sha256(encoded([VERSION, [(i, job['assets'][str(i)]['sha']) for i in step['indices']]])).hexdigest()
        if expected != step['key']:
            raise Pending('批次结果与来源图片 SHA 不一致')
        for value in result:
            r = enrich.ImageRead.model_validate(value)
            if r.index in reads:
                raise Pending('检查点含重复图片编号')
            reads[r.index] = r
    count = len(art['images'])
    if any(i < 0 or i >= count for i in reads):
        raise Pending('检查点与图片清单不一致')
    while len(reads) < count:
        if work_budget.remaining() < config.IMAGE_LLM_TIMEOUT + 70:
            raise Pending('本轮时间用完，已完成批次保存，自动继续')
        remaining = [i for i in range(count) if i not in reads]
        indices, images = [], []
        for index in remaining[:job.get('batch_size', BATCH_IMAGES)]:
            image = asset(url, job, index, art['images'][index])
            # Base64 plus a conservative prompt reserve.
            size = request_size([*images, image])
            if size > MAX_REQUEST_BYTES and images:
                break
            if size > MAX_REQUEST_BYTES:
                raise Pending(f'图 {index+1} 编码后超过单次请求上限')
            indices.append(index); images.append(image)
        say(f'⏳ 已完成 {len(reads)}/{count} 张，正在读图 {indices[0]+1}–{indices[-1]+1}…')
        try:
            result = read_batch(art['title'], art['text'], indices, images)
        except Pending:
            if len(indices) > 1:
                checkpoint(url, job, batch_size=max(1, len(indices)//2))
            raise
        key = hashlib.sha256(encoded([VERSION, [(i, job['assets'][str(i)]['sha']) for i in indices]])).hexdigest()
        uri = put_json(url, 'batch', [r.model_dump() for r in result])
        checkpoint(url, job, steps=[*job.get('steps', []), {'key': key, 'indices': indices, 'uri': uri}],
                   completed=len(reads)+len(result), failure_streak=0)
        reads.update({r.index: r for r in result})
    return [reads[i] for i in range(count)]


def summarize(url, job, art, reads, note):
    from . import pipeline, tag_store
    if job.get('writing_started') and job.get('summary_uri'):
        return enrich.Enrichment.model_validate(get_json(job['summary_uri']))
    vocabulary = tag_store.load()
    context = hashlib.sha256(encoded([VERSION, vocabulary, [(r.index, r.model_dump()) for r in reads], note, art.get('comments')])).hexdigest()
    if job.get('summary_context') == context and job.get('summary_uri'):
        return enrich.Enrichment.model_validate(get_json(job['summary_uri']))
    if work_budget.remaining() < config.IMAGE_LLM_TIMEOUT + 65:
        raise Pending('逐图读取已完成，整体汇总将在下一轮继续')
    evidence = art['text'] + '\n' + '\n'.join(r.text for r in reads if r.informative)
    items = [f'图 {r.index+1}：{r.summary}\n识别说明：{r.note}\nOCR 原文片段：{r.text[:800]}'
             for r in reads if r.informative]
    pieces = [art['text'], *items]
    # All image summaries are represented; oversized sets are reduced with saved text calls.
    merged = dict(job.get('merge_steps', {}))
    while len('\n\n'.join(pieces)) > enrich.SUMMARY_INPUT_CHARS:
        groups, current, size = [], [], 0
        for piece in pieces:
            if len(piece) > 20000:
                raise Pending('单图总结或帖子正文超过汇总容量，需人工检查')
            if current and size + len(piece) > 20000:
                groups.append(current); current=[]; size=0
            current.append(piece); size += len(piece)+2
        if current: groups.append(current)
        next_pieces=[]
        for group in groups:
            if work_budget.remaining() < config.IMAGE_LLM_TIMEOUT+65:
                raise Pending('整体汇总分段进度已保存，下轮继续')
            key = hashlib.sha256(encoded([context, group])).hexdigest()
            if key not in merged:
                value, error = enrich.enrich(art['title'], '\n\n'.join(group), enrich.POST_SUMMARY_HINT+'部分图片汇总，非全文', request_timeout=config.IMAGE_LLM_TIMEOUT, disable_thinking=True, evidence_text=evidence)
                if error: raise Pending(error)
                merged[key] = put_json(url, 'merge', value.model_dump())
                checkpoint(url, job, merge_steps=merged)
            value = get_json(merged[key])
            next_pieces.append(value['overview']+'\n'+'\n'.join(value['key_points']))
        if len('\n\n'.join(next_pieces)) >= len('\n\n'.join(pieces)):
            raise Pending('整体汇总未能缩小输入，进度保留待检查')
        pieces = next_pieces
    value, error = enrich.enrich(art['title'], '\n\n'.join(pieces),
                                enrich.POST_SUMMARY_HINT+f'完整帖子：{len(reads)} 张图全部已读取，以下为按序识别结果。'+url,
                                note='',
                                request_timeout=config.IMAGE_LLM_TIMEOUT, disable_thinking=True, evidence_text=evidence,
                                **({'comments': art['comments']} if art.get('comments') else {}))
    if error: raise Pending(error)
    value.images = reads
    checkpoint(url, job, summary_context=context, summary_uri=put_json(url, 'summary', value.model_dump()))
    return value


def schedule(url, job, *, delay=1):
    delay = max(10, delay)
    number = job.get('round', 0)+1
    checkpoint(url, job, round=number, next_retry_at=time.time()+max(delay, 300))
    return tasks.enqueue_images(url, number, delay)


def finish(url, job, art, platform, msg, note, en):
    from . import pipeline, notion, chat_notes, telegram
    page = job.get('page')
    if not page:
        checkpoint(url, job, writing_started=True)
        # Find a page whose POST succeeded but whose response/checkpoint was lost.
        matches = notion._call('POST', f'/databases/{config.NOTION_MATERIAL_DB}/query',
                              {'filter': {'property': '原始链接', 'url': {'equals': url}}})['results']
        if len(matches)>1: raise Pending('同链接存在多份资料页，停止自动写入')
        if matches:
            page = {'id':matches[0]['id'], 'url':matches[0]['url']}
            if notion.prop_text(matches[0], '处理状态') != '需处理':
                raise Pending('发现未登记的已完成页面，需核对后继续')
        else:
            page = notion.create_material(title=art['title'], material_type=en.material_type or '帖子', platform=platform,
                                           url=url, topics=en.topics, processing='需处理', blocks=[])
            page = {'id':page['id'], 'url':page['url']}
        checkpoint(url, job, page=page)
    if not job.get('body_verified'):
        source = [f'原始链接：{url}', f'收集时间：{job["imported_at"]}', f'原文归档：{job["body_uri"]}',
                  *pipeline._capture_source_lines(art),
                  f'图片：{len(art["images"])} 张，全部分批读取并归档',
                  *[f'图 {i+1} 原件：{job["assets"][str(i)]["original_uri"]}' for i in range(len(art['images']))]]
        blocks = pipeline._web_blocks(url, en, source, art['text'], len(art['images']), art.get('comments'))
        blocks.append(notion.heading(pipeline.NOTE_SECTION))
        link = telegram.note_deeplink(page['id'])
        if link: blocks.append(notion.link_callout(pipeline.NOTE_ENTRY_PREFIX, pipeline.NOTE_ENTRY_LABEL, link))
        chat_notes._append_prefix(page['id'], blocks)
        current = notion.get_page(page['id'])
        notion.refresh_machine_fields(page['id'], title=art['title'], material_type=en.material_type or '帖子',
                                      topics=en.topics, overview=en.overview, processing='完成', existing_page=current)
        checkpoint(url, job, body_verified=True)
    rid = store.put_record(msg['chat']['id'], msg['message_id'], kind='material', notion_page_id=page['id'],
                           notion_url=page['url'], url=url)
    store.index_url(url, rid)
    pipeline.explicit_relations.bind_material(msg, page['id'], url)
    # Link supplements are ignored, including checkpoints from older jobs.
    pipeline._claim_staged(url, page['id'])
    checkpoint(url, job, status='done', error='', completed=len(art['images']))
    if job.get('update_id') is not None:
        store.set_update_status(job['update_id'], 'done')
        store.unpark(job['update_id'], url)
    try:
        art_data = pipeline.thumbnails.material_art(en.material_type, platform, url,
                            fallback_category=en.fallback_category, candidates=art.get('art_candidates') or {})
        if art_data and work_budget.remaining()>20:
            with work_budget.limit(min(10, work_budget.remaining()-15)):
                notion.decorate_page(page['id'], **art_data)
    except Exception:
        log.info('batch image decoration skipped; material verified')
    return pipeline._success_text('✅ 已整理', art['title'], en.overview, source_url=url, platform=platform,
                                  tag_review=en.tag_review), page['url']


def handle(msg, url, art, platform, note, say, update_id=None, *, require_lease=False, manual=False):
    from . import pipeline
    owner = store.claim_image_job(url)
    if not owner:
        if require_lease:
            raise Busy('图片任务仍由其他请求处理，稍后重试')
        return '⏳ 这条资料正在分批处理，已完成进度会保留。', ''
    job = store.get_image_job(url) or {}
    starting_progress = progress(job)
    try:
        if job.get('status') == 'done':
            if pipeline.notion.page_alive(job['page']['id']):
                return '♻️ 已保存过', job['page']['url']
            # A deleted page must be freshly importable, with the new message
            # and source. Preserve the old job as an immutable archive.
            history = put_json(url, 'previous-job', job)
            checkpoint(url, job, previous_job_uri=history, snapshot_uri=None,
                       assets={}, steps=[], merge_steps={}, summary_uri=None,
                       summary_context=None, page=None, body_verified=False,
                       writing_started=False, failure_streak=0, completed=0)
            starting_progress = progress(job)
        elif auto_paused(job) and not manual:
            return '⏸ 图片任务已暂停自动重试，已保存进度。可用 /retry 手动重试。', ''
        if not job.get('snapshot_uri'):
            checkpoint(url, job, url=url, msg=msg, update_id=update_id, platform=platform, note=note,
                       status='processing', version=VERSION, imported_at=pipeline._now(), completed=0,
                       snapshot_uri=put_json(url, 'source', art), batch_size=BATCH_IMAGES,
                       body_uri=archive_body(url, art['text']))
        else:
            art = get_json(job['snapshot_uri'])
            msg, note, platform = job['msg'], job.get('note',''), job['platform']
            if platform == '小红书':
                images = extract.repair_xhs_image_urls(art['images'], art.get('thumbnail') or '')
                if images != art['images']:
                    art['images'] = images
                    art['art_candidates'] = {**art.get('art_candidates', {}), 'body': images}
                    checkpoint(url, job, snapshot_uri=put_json(url, 'source', art))
        if not job.get('body_uri'):
            checkpoint(url, job, body_uri=archive_body(url, art['text']))
            if not job.get('body_uri'): raise Pending('帖子正文归档失败，需重试')
        checkpoint(url, job, status='processing')
        reads = read_all(url, job, art, say)
        say(f'⏳ {len(reads)} 张图已全部读取，正在汇总整帖…')
        en = summarize(url, job, art, reads, note)
        if work_budget.remaining()<25: raise Pending('识别和汇总已保存，下轮写入 Notion')
        return finish(url, job, art, platform, msg, note, en)
    except (Pending, enrich.EndpointUnavailable, work_budget.Exhausted) as exc:
        error = record_failure(url, job, starting_progress, str(exc))
        pipeline._stage(msg, url, error, note, '分批读图',
                        material_title=(art or {}).get('title', ''), material_text=(art or {}).get('text', ''))
        if job.get('snapshot_uri') and not auto_paused(job):
            schedule(url, job, delay=30 if job['failure_streak'] else 1)
        count = len(art['images']) if art else '?'
        return (f'⏳ 已完成 {job.get("completed",0)}/{count} 张，进度已保存。\n'
                '原因：' + pipeline.telegram.esc(error[:180])), ''
    except Exception as exc:
        error = record_failure(url, job, starting_progress, pipeline.telegram.redact(str(exc)))
        pipeline._stage(msg, url, error, note, '分批入库',
                        material_title=(art or {}).get('title', ''), material_text=(art or {}).get('text', ''))
        raise
    finally:
        store.release_image_job(url, owner)


def resume(url, *, notify=True, manual=False, say=None):
    from . import pipeline, telegram
    job = store.get_image_job(url)
    if not job or job.get('status') == 'done' or (auto_paused(job) and not manual):
        return {'ok': True, 'skipped': True}
    if job.get('lease_expires', 0) > time.time():
        return {'ok':False, 'busy':True}
    update = store.get_update(job['update_id']) if notify and job.get('update_id') is not None else {}
    ack = (update or {}).get('ack_message_id')
    last = [0.0]
    def report(text):
        if say:
            say(text)
        if notify and ack and time.monotonic()-last[0]>25:
            last[0] = time.monotonic()
            try: telegram.edit(job['msg']['chat']['id'], ack, text)
            except Exception: pass
    with work_budget.limit(260):
        try:
            text, notion_url = handle(job['msg'], url, None, job['platform'],
                                      job.get('note', ''), report, job.get('update_id'),
                                      require_lease=True, manual=manual)
        except Busy:
            return {'ok':False, 'busy':True}
        if notify and ack:
            try: telegram.edit(job['msg']['chat']['id'], ack, text, notion_url=notion_url or None, parse_mode='HTML')
            except Exception: pass
    return {'ok':True, 'completed':bool(notion_url)}


def drain():
    sent=0
    for job in store.pending_image_jobs():
        if (auto_paused(job) or not job.get('snapshot_uri')
                or job.get('next_retry_at', 0) > time.time() or job.get('lease_expires', 0) > time.time()):
            continue
        owner = store.claim_image_job(job['url'])
        if not owner: continue
        try:
            current = store.get_image_job(job['url'])
            if current.get('status') != 'done' and not auto_paused(current):
                sent += bool(schedule(job['url'],current,delay=1))
        finally: store.release_image_job(job['url'],owner)
    return sent
