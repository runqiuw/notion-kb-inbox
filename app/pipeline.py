"""Ingestion pipeline: one Telegram update -> one Notion page.

Routing (plan sections 12-14):
    text + URL            -> Material
    text, reply to a known material -> Thought linked to it
    text alone            -> standalone Thought
    photo                 -> Material(图片) with OCR
    album (media_group)   -> one Material(图片) for all of its photos
    PDF / document        -> Material(论文)
    edited_message        -> rewrite the Thought we made from the original
"""
import datetime
import logging
import re
import time
import uuid

from . import chat_notes, chat_share, enrich, extract, notion, settings, storage, store, telegram
from . import config, paper_project, render_web, thumbnails, work_budget, share_text, comments, explicit_relations, jina_reader

log = logging.getLogger(__name__)

# Share sheets paste their own blurb alongside the link. That text is the
# platform talking, not the user, and a knowledge base that files it as 随想
# is putting words in the user's mouth (plan section 29.3).
#
# Whole-template phrases only. Bare platform names used to be in here -- a
# thought that merely mentioned 小红书 was thrown away, and losing something the
# user actually wrote is far worse than filing something they did not. The long
# tail of templates (豆包, 公众号, X...) is not chased here either: that list can
# never be finished, and enrich judges it with the article in hand.
_SHARE_BOILERPLATE = (
    "这段文字复制", "打开【小红书】", "复制打开抖音", "复制此链接",
    "打开抖音搜索", "点击链接直接打开", "长按复制", "@小红书",
    "发布了一篇小红书笔记",
)

# A message that *is* nothing but a platform's name is the share sheet's
# signature, not a thought -- matched whole, never as a substring, so that
# "bilibili 的弹幕文化挺有意思" survives while a bare "- 哔哩哔哩" does not.
_PLATFORM_ONLY = frozenset((
    "小红书", "哔哩哔哩", "bilibili", "抖音", "知乎", "微博",
    "网页链接", "查看全文", "打开app",
))


def user_note(text: str, material_title: str = "", material_text: str = "") -> str:
    """Whatever the user actually wrote alongside a link, or "".

    Anything that is share-sheet furniture, or that merely repeats what the
    material already says, is not a thought -- only the user's own words are.
    """
    if extract.find_urls(text):
        return ""  # Temporary policy: link-bearing captions are not thoughts.
    decision = share_text.classify(text, material_title, material_text)
    note = decision['candidate'] if decision['state'] == 'user' else ''
    if not note:
        return ""
    bare = re.sub(r"[\s\-—·、，,。.!！?？:：\[\]【】]+", "", note).lower()
    if bare in _PLATFORM_ONLY:
        return ""
    squashed = re.sub(r"\s+", "", note)
    if len(squashed) < 4:
        return ""
    # Source-only fragments were removed above. A long quotation followed by a
    # short real comment must not disappear because most characters are quoted.
    return note


def captured_note(url: str, text: str) -> str:
    """Use the same source context for a duplicate or waiting image URL."""
    if not text.strip():
        return ''
    job = store.get_image_job(url) if config.GCP_PROJECT else None
    if job and job.get('snapshot_uri'):
        from . import image_batches
        art = image_batches.get_json(job['snapshot_uri'])
        return user_note(text, art['title'], art['text'])
    return user_note(text)


_SHINGLE = 8
_ECHO_RATIO = 0.6


def _mostly_echo(squashed: str, *haystacks: str) -> bool:
    """True when the message is largely the material read back to us.

    An exact-substring test was too literal to be useful: a share sheet pastes
    the title, then a slab of the body, then its own tail, and the result is a
    substring of nothing. Comparing in overlapping chunks asks the question
    that actually matters -- how much of this did the user add? Below the
    threshold it is the platform talking.
    """
    hay = re.sub(r"\s+", "", " ".join(h or "" for h in haystacks))
    if not hay or len(squashed) < _SHINGLE:
        return bool(hay) and squashed in hay
    chunks = [squashed[i:i + _SHINGLE]
              for i in range(0, len(squashed) - _SHINGLE + 1)]
    hits = sum(1 for c in chunks if c in hay)
    return hits / len(chunks) >= _ECHO_RATIO
MAX_INLINE_CHARS = 2500   # longer bodies get folded into a toggle


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _body_blocks(overview: str, raw_text: str, raw_heading: str,
                 source_lines: list[str], extra: list[dict] | None = None) -> list[dict]:
    blocks: list[dict] = []
    if overview:
        blocks.append(notion.heading("中文概览"))
        blocks.extend(notion.paragraphs(overview))
    if extra:
        blocks.extend(extra)
    if raw_text:
        blocks.append(notion.heading(raw_heading))
        paras = notion.paragraphs(raw_text)
        if len(raw_text) <= MAX_INLINE_CHARS:
            blocks.extend(paras)
        else:
            # Keep the opening unfolded so it stays scannable; fold the rest.
            blocks.extend(paras[:2])
            rest = paras[2:]
            for i in range(0, len(rest), notion.MAX_CHILDREN):
                blocks.append(notion.toggle(
                    "展开全文" if i == 0 else f"展开全文（续 {i // notion.MAX_CHILDREN + 1}）",
                    rest[i:i + notion.MAX_CHILDREN]))
    if source_lines:
        blocks.append(notion.toggle("来源与定位", notion.bullets(source_lines)))
    return blocks


def _folded_blocks(label: str, children: list[dict]) -> list[dict]:
    """Split long originals without losing children to Notion's per-block cap."""
    return [notion.toggle(label if i == 0 else f"{label}（续 {i // notion.MAX_CHILDREN + 1}）",
                          children[i:i + notion.MAX_CHILDREN])
            for i in range(0, len(children), notion.MAX_CHILDREN)]


def _image_summary_blocks(en: enrich.Enrichment, image_count: int) -> list[dict]:
    blocks: list[dict] = []
    reads = {im.index: im for im in en.images if 0 <= im.index < image_count}
    informative = [(i, reads[i]) for i in sorted(reads) if reads[i].informative]
    if informative:
        blocks.append(notion.heading("逐图总结"))
        for i, im in informative:
            blocks.append(notion.heading(f"图 {i + 1}", level=3))
            blocks.extend(notion.paragraphs(im.summary.strip() or "本图总结暂未生成，请展开转写核对。"))
            if im.note.strip():
                blocks.extend(notion.paragraphs(f"识别说明：{im.note.strip()}"))
            if im.text.strip():
                blocks.extend(_folded_blocks(f"图 {i + 1} · OCR 原文（机器转写，可能有误）",
                                             notion.paragraphs(im.text)))
    skipped = [(i, reads[i]) for i in sorted(reads) if not reads[i].informative]
    if skipped:
        blocks.extend(_folded_blocks(
            f"跳过了 {len(skipped)} 张装饰性图片",
            notion.bullets([f"图 {i + 1}：{im.note or '无实质信息'}" for i, im in skipped])))
    return blocks


def _web_blocks(url: str, en: enrich.Enrichment, source_lines: list[str],
                raw_text: str = "", image_count: int = 0, comment_capture: dict | None = None) -> list[dict]:
    """Summaries first; all source text stays available in collapsed toggles."""
    blocks = _body_blocks(en.overview, "", "", [])
    if en.key_points:
        blocks.extend([notion.heading("中文要点"), *notion.bullets(en.key_points)])
    blocks.extend(_image_summary_blocks(en, image_count))
    blocks.extend(comments.blocks(en, comment_capture))
    blocks.extend(_folded_blocks("原始正文（展开查看）", notion.paragraphs(raw_text)))
    if source_lines:
        blocks.append(notion.toggle("来源与定位", notion.bullets(source_lines)))
    return blocks


def _web_hint(url: str, platform: str) -> str:
    hint = f"来自 {platform}：{url}"
    if platform in {"网页", "微信"}:
        return enrich.WEB_SUMMARY_HINT + hint
    if platform in {"小红书", "知乎"}:
        return enrich.POST_SUMMARY_HINT + hint
    return hint


VIDEO_DESCRIPTION_SCOPE = "仅保存发布者简介，未解析视频画面或音轨。"


def _is_xhs_video(platform: str, art: dict) -> bool:
    return platform == "小红书" and art.get("media_type") == "video"


def _video_description_error(art: dict) -> str:
    # Hashtags and an echoed title alone cannot support a reading summary.
    text = re.sub(r"#[^\s#]+", "", art.get("text") or "")
    text = text.replace((art.get("title") or "").strip(), "") if art.get("title") else text
    return ("视频简介缺少实质内容（只有标题、话题标签或过短），未解析画面或音轨，继续暂存"
            if len(re.findall(r"[\w]", text)) < 30 else "")


def _video_description_hint(url: str) -> str:
    return (enrich.WEB_SUMMARY_HINT + f"小红书视频的发布者简介：{url}。"
            + VIDEO_DESCRIPTION_SCOPE
            + "仅依据以下简介生成概览和要点，概览注明依据发布者简介；"
              "作者的数字和效果声明归于作者，不视为核实事实；不得推测视频画面、音轨或演示结果。")


def _video_description_blocks(en: enrich.Enrichment, art: dict, sources: list[str]) -> list[dict]:
    return [notion.callout(VIDEO_DESCRIPTION_SCOPE, "ℹ️"),
            *_body_blocks(en.overview, "", "", []),
            notion.heading("中文要点"), *notion.bullets(en.key_points),
            *_folded_blocks("发布者简介原文（展开查看）", notion.paragraphs(art["text"])),
            notion.toggle("来源与定位", notion.bullets(sources))]


def _abstract_blocks(abstract: str, translation: str) -> list[dict]:
    """Keep the complete original and translation adjacent without shortening."""
    return [notion.heading("Abstract · 中英对照"),
            notion.callout("中文译文由 AI 翻译，摘要原文保留如下。", "🤖"),
            notion.heading("原文", level=3), *notion.paragraphs(abstract),
            notion.heading("中文译文", level=3), *notion.paragraphs(translation)]


def _project_blocks(url: str, en: enrich.Enrichment, meta: dict,
                    sources: list[str]) -> list[dict]:
    blocks = _web_blocks(url, en, [])
    blocks.extend(_abstract_blocks(meta["text"], en.abstract_zh))
    blocks.append(notion.toggle("来源与定位", notion.bullets(sources)))
    return blocks


NOTE_SECTION = "我的想法"
NOTE_ENTRY_PREFIX = "读完想说点什么？"
NOTE_ENTRY_LABEL = "记下想法"


def _create_material(**kw) -> dict:
    """Create the material, then open its 「我的想法」 section.

    The section heading goes in at creation, empty apart from the entry link,
    so that every note mirrored later lands underneath it. The alternative --
    adding the heading alongside the first note -- would mean reading the whole
    page each time just to find out whether it is already there.

    The link has to carry the page id, which does not exist until the page
    does, hence an append rather than one more block in the body. A page
    without the section is still a correct page, so a failure here is logged
    and dropped rather than losing the material that was just saved.
    """
    # Decorative writes happen on the known page ID, never by retrying creation.
    # Thus a rejected upload/icon or a lost PATCH response cannot create a second
    # material. The basic emoji/type fallback is already present at creation.
    art = kw.pop("art", {})
    messages = kw.pop("messages", [])
    thumbnail_url = kw.pop("thumbnail_url", "")
    if thumbnail_url:
        art.setdefault("candidates", {}).setdefault("shares", []).append(thumbnail_url)
    page = notion.create_material(**kw)
    # Checkpoint before any slow optional image work. In a retry the existing
    # URL resolves to this page even if decoration was interrupted.
    for msg in messages:
        fields = {"kind": "material", "notion_page_id": page["id"], "notion_url": page["url"]}
        if kw.get("url"):
            fields["url"] = kw["url"]
        rid = store.put_record(msg["chat"]["id"], msg["message_id"], **fields)
        if kw.get("url"):
            store.index_url(kw["url"], rid)
        explicit_relations.bind_material(msg, page['id'], kw.get('url') or '')
    tail = [notion.heading(NOTE_SECTION)]
    link = telegram.note_deeplink(page["id"])
    if link:
        tail.append(notion.link_callout(NOTE_ENTRY_PREFIX, NOTE_ENTRY_LABEL, link))
    try:
        notion.append_blocks(page["id"], tail)
    except Exception as e:
        log.warning("could not append the note section: %s", e)
    try:
        visuals = thumbnails.material_art(kw["material_type"], kw["platform"], kw.get("url") or "", **art)
        seconds = min(10, work_budget.remaining() - 15)
        if visuals and seconds > 5:
            with work_budget.limit(seconds):
                notion.decorate_page(page["id"], **visuals)
    except Exception:
        log.warning("material decoration unavailable; content already saved")
    return page


_PLATFORM_TYPE = {"小红书": "帖子", "知乎": "帖子", "微信": "网页",
                  "B站": "视频", "YouTube": "视频", "论文": "论文"}


def _type_for(platform: str) -> str:
    """What a link from this platform is, when the model could not say.

    Better than the generic 其他: the platform already tells us most of it, and
    a material filed as 其他 drops out of the 文章/论文/视频 tabs on the home
    page even though we know perfectly well which one it belongs in.
    """
    return _PLATFORM_TYPE.get(platform, "网页")


# B站/YouTube are only complete with their transcript. Scraping gets a title
# or a description at best, and a long description passes any length check
# while still not being the video -- so videos run as durable jobs in video.py.
VIDEO_PLATFORMS = {"YouTube", "B站"}


def _capture_source_lines(art: dict) -> list[str]:
    capture = art.get('capture') or {}
    if capture.get('method') != 'jina_reader_anonymous':
        return []
    return ['正文读取：Jina Reader 免费匿名接口（第三方读取）',
            f'正文读取时间：{capture["fetched_at"]}',
            f'阅读接口原始返回归档：{capture["raw_archive"]}']


def fetch_for(url: str, platform: str = "", say=None) -> dict:
    """Get the readable text behind a URL, by whichever route suits the source.

    One function because there used to be two. The 公众号 branch was added to
    the ingestion path only, so /retry kept sending those links through the
    generic extractor and kept failing in exactly the way the branch existed to
    prevent. Any future per-source route has to land here, once.
    """
    say = say or (lambda _t: None)
    platform = platform or extract.guess_platform(url)

    started = time.monotonic()
    if platform in VIDEO_PLATFORMS:
        from . import video
        return video.fetch_text(url)

    if platform == "微信":
        # 公众号 leaves <title> empty for JS to fill and hides the body behind
        # `visibility: hidden`, so a generic extractor gets a partial answer.
        return extract.fetch_wechat(url)

    if extract.plain_http_blocked(url):
        # Sites that 403 a plain GET skip it rather than burn a certain failure.
        art = {"ok": False, "title": "", "text": "",
               "error": f"{platform} 不接受普通 HTTP 抓取"}
    else:
        art = extract.fetch_article(url)

    # Render only confirmed dynamic shells on the ordinary public web path.
    # Specific platforms, HTTP errors, access gates and short articles do not
    # enter this branch. Static and rendered HTML use exactly the same parser.
    if (not art["ok"] and art.get("failure_kind") == "dynamic_shell"
            and platform == "网页" and not chat_share.is_share(url)
            and not extract.arxiv_id(url) and not extract.doi_from_url(url)
            and not extract.parsehub_platform(url) and config.DYNAMIC_WEB_ENABLED):
        reserve = max(120, config.LLM_TIMEOUT + 60)
        render_seconds = min(config.DYNAMIC_WEB_TIMEOUT, work_budget.remaining()-reserve)
        if render_seconds < 3:
            art.update(failure_kind="budget_exhausted", error="本次剩余时间不足以加载并整理，留待下次重试")
        else:
            say("⏳ 正在加载动态网页…")
            rendered = render_web.render(url, timeout=render_seconds)
            if rendered["ok"]:
                art = extract.parse_article_html(rendered["html"], rendered["url"])
                art["fetch_path"] = "rendered"
            else:
                art.update(error=rendered["error"], failure_kind=rendered["failure_kind"])
            log.info("web-fetch path=rendered ok=%s failure_kind=%s seconds=%.3f peak_rss_bytes=%s",
                     art["ok"], art.get("failure_kind", ""), time.monotonic()-started,
                     rendered.get("peak_rss_bytes", 0))
            return art

    # Fallback: a per-platform parser. It reads the state blob a page embeds or
    # reimplements the site's request signature, so it breaks whenever the site
    # changes -- treat a failure as routine, not exceptional.
    if not art["ok"] and extract.parsehub_platform(url):
        say(f"⏳ 普通抓取没拿到，改用 {platform} 专用解析器…")
        fb = extract.parse_via_parsehub(url, extract.parse_cookie_for(url))
        if fb["ok"]:
            art = {"ok": True, "title": fb["title"], "text": fb["text"],
                   "error": "", "images": fb.get("images") or [],
                   "thumbnail": fb.get("thumbnail") or "",
                   "media_type": fb.get("media_type") or "",
                   "videos": fb.get("videos") or [],
                   "resolved_url": fb.get("resolved_url") or url,
                   "art_candidates": fb.get("art_candidates") or {
                       "shares": [fb["thumbnail"]] if fb.get("thumbnail") else [],
                       "body": fb.get("images") or []}}
        else:
            art = {"ok": False, "title": "", "text": "",
                   "error": f"{art['error']}；专用解析器也失败：{fb['error']}"}
    if not art['ok'] and config.JINA_READER_ENABLED and jina_reader.canonical_url(url):
        say('⏳ 知乎专用读取未成功，正在尝试免费备用入口…')
        fallback = jina_reader.fetch(url)
        if fallback['ok']:
            capture = fallback['capture']
            raw = fallback.pop('reader_response')
            archive = storage.archive(raw.encode('utf-8'),
                                      f'{store.url_hash(url)}-jina-{capture["response_sha256"][:20]}.md',
                                      'text/markdown; charset=utf-8')
            if archive:
                capture['raw_archive'] = archive
                art = fallback
            else:
                art['error'] += '；备用入口取得正文，但原始返回归档失败，保留待重试'
        else:
            art['error'] += '；' + fallback['error']
    log.info("web-fetch path=%s ok=%s failure_kind=%s seconds=%.3f", art.get('fetch_path', 'static'), art["ok"],
             art.get("failure_kind", ""), time.monotonic()-started)
    return art


def _tag_review_text(review: dict | None) -> str:
    if not review or review.get('needed') is not True:
        return ''
    reason = str(review.get('reason') or '分类依据不足，需确认。')[:400]
    lines = ['标签待确认：' + telegram.esc(reason)]
    suggestions = review.get('suggestions') or []
    if suggestions:
        lines.append('建议标签：' + telegram.esc(' / '.join(str(t) for t in suggestions[:3])))
    else:
        lines.append('建议：补充来源内容或确认合适的标签范围。')
    return '\n'.join(lines)


def _success_text(lead: str, title: str, overview: str = "", *,
                  source_url: str = "", platform: str = "", tag_review: dict | None = None) -> str:
    """The "已整理" reply, formatted as Telegram HTML: bold title, a monospace
    peek at the overview, and -- when the material came from somewhere other
    than what the user just typed at the bot -- a link back to it. Every
    dynamic piece goes through telegram.bold/mono/link, which escape it, since
    a title or overview can carry '<', '>' or '&' straight from the source
    page."""
    lines = [lead, telegram.bold(title)]
    if overview:
        lines.append(telegram.mono(overview[:100]))
    if source_url:
        label = f"来源：{platform}" if platform else "来源"
        lines.append(telegram.link(label, source_url))
    review_text = _tag_review_text(tag_review)
    if review_text:
        lines.append(review_text)
    return "\n".join(lines)


def _short_error(error: str, limit: int = 120) -> str:
    """One readable line: httpx appends an MDN link and newlines to every
    status error, which buries the actual reason in a Telegram reply."""
    text = re.sub(r"\s*For more information check: [!-~]+", "", error or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + "…" if len(text) > limit else text


def _save_link_thought(msg: dict, url: str, note: str,
                       map_message: bool = False,
                       related: list[str] | None = None) -> dict:
    """Checkpoint a link's thought before staging, independently of the material.

    records merge fields, so link_thought survives /retry mapping the original
    message to a material. Repeated handoffs reuse the checkpoint's page ID.
    """
    chat_id, message_id = msg["chat"]["id"], msg["message_id"]
    saved = store.get_record(chat_id, message_id) or {}
    note_title = re.sub(r'\s+', ' ', note).strip()[:60]
    related = explicit_relations.note_targets(msg)
    raw = msg.get('text') or msg.get('caption') or ''
    residual = extract.without_first_url(raw, keep_label=False).strip()
    decision = share_text.classify(residual)
    original_lines = [line.strip() for line in residual.splitlines() if line.strip()]
    candidate_lines = [line.strip() for line in decision['candidate'].splitlines() if line.strip()]
    if raw and decision['state'] == 'user' and candidate_lines == original_lines:
        note = raw.strip()
    page = saved.get("link_thought")
    if not page:
        if saved.get("kind") == "thought" and saved.get("notion_page_id"):
            # Reuse notes saved by the queued-link path before checkpoints.
            page = {"id": saved["notion_page_id"], "url": saved["notion_url"]}
        else:
            page = notion.create_thought(
                title=note_title, note_type="阅读笔记" if related else "随想", topics=[], related=related or [], sync_body=True,
                blocks=notion.paragraphs(note),
            )
        # Persist immediately, before staging or any further Notion reads.
        page = {"id": page["id"], "url": page["url"], 'text': note,
                'preserve_full_message': bool(raw and note == raw.strip()),
                'association_policy': 'explicit_reply'}
        store.put_record(chat_id, message_id, link_thought=page)
    elif related:
        # A staged thought can already exist when this material finally lands.
        # Reuse it and finish its relation/mirror, rather than create a second.
        explicit_relations.relate(page["id"], related)
    explicit_relations.queue_note(msg, page['id'], related)
    if map_message and (saved.get("kind") != "thought" or
                        saved.get("notion_page_id") != page["id"]):
        container, body_ids = notion.body_location(page)
        store.put_record(chat_id, message_id, kind="thought",
                         notion_page_id=page["id"], notion_url=page["url"],
                         body_block_ids=body_ids, body_parent_id=container, text=note)
    return page


def _stage(msg: dict, url: str, error: str, note_text: str,
           what: str, *, material_title: str = '', material_text: str = '') -> tuple[str, str]:
    """Stage an unreadable link; keep existing note references, ignore new text."""
    thought = (store.get_record(msg['chat']['id'], msg['message_id']) or {}).get('link_thought')
    attempts = store.stage_url(url, msg["chat"]["id"], msg["message_id"], error,
                               [thought["id"]] if thought else [])
    lines = [f"⏸ {what}没拿到，这条暂不进知识库",
             f"已放进暂存区（第 {attempts} 次尝试），发 /retry 再试",
             f"原因：{telegram.esc(_short_error(error) or '未知')}"]
    if thought:
        lines.append("你的文字已作为独立笔记保存；资料入库不会自动关联。")
    return "\n".join(lines), thought["url"] if thought else ""


def _claim_staged(url: str, page_id: str) -> None:
    """A staged link just made it in: relate and mirror its waiting thoughts.

    A link that fails (Notion hiccup) keeps the entry, now pointing at the page,
    so /retry can finish the job. Dropping it would leave the thought unrelated
    with nothing left that remembers it should not be.
    """
    staged = store.get_staged(url)
    if not staged:
        return
    pending = []
    for tid in explicit_relations.ids(staged.get("explicit_thought_ids")):
        try:
            explicit_relations.relate(tid, [page_id])
        except Exception as e:
            log.warning("could not finish staged thought %s: %s", tid, e)
            pending.append(tid)
    if pending:
        store.keep_staged_links(url, page_id, pending)
        store.update_chat_stage(url, explicit_thought_ids=pending)
    else:
        store.unstage(url)


# ---------- handlers ----------

def _read_url_images(art: dict, url: str, say) -> tuple[list, list[str], list[str]]:
    """Archive source bytes before summarizing, and account for every image."""
    urls = art.get("images") or []
    if not urls:
        return [], [], []
    say(f"⏳ 这条带 {len(urls)} 张图，正在下载…")
    blobs, missing = extract.download_images(urls)
    originals = getattr(blobs, "originals", [(i, b, mime) for i, (b, mime) in enumerate(blobs)])
    stored = []
    for i, data, mime in originals:
        ext = mime.split("/")[-1] if mime.startswith("image/") else "bin"
        stored.append(storage.archive(data, f"{store.url_hash(url)}-{i}.{ext}", mime))
    return blobs, stored, missing


def _image_sources(blobs: list, stored: list[str]) -> list[str]:
    if not blobs:
        return []
    return [f"图片：{len(blobs)} 张，已归档 {sum(bool(x) for x in stored)} 张",
            *[f"图片原件归档：{uri}" for uri in stored if uri]]


def _already_saved(msg: dict, url: str, existing: dict, note_text: str) -> tuple[str, str]:
    explicit_relations.bind_material(msg, existing['notion_page_id'], url)
    # Dedupe must never lose a new thought (plan section 13) -- but share
    # boilerplate is not a thought.
    note_text = captured_note(url, note_text)
    if note_text.strip():
        page = _save_link_thought(msg, url, note_text.strip(), map_message=True,
                                  related=[existing["notion_page_id"]])
        return "♻️ 这条链接之前存过；你这次的想法已经作为新笔记接上了。", page["url"]
    return "♻️ 已保存过", existing.get("notion_url", "")


def _handle_video(msg: dict, url: str, note_text: str, say, update_id: int | None) -> tuple[str, str]:
    """Videos dedupe by entity (youtube:ID, bilibili:BV:cid), not link spelling,
    and run as durable jobs: transcripts can take many requests or hours."""
    from . import video
    platform = extract.guess_platform(url)
    say(f"⏳ 正在识别 {platform} 视频…")
    try:
        ref = video.identify(url)
    except (video.GiveUp, video.Pending) as e:
        alert = getattr(e, "alert", False)
        if alert:
            log.error("VIDEO_ALERT identify url=%s reason=%s", url, e)
        text, link = _stage(msg, url, str(e), note_text, f"{platform} 视频信息")
        return text + ("\n⚠️ B 站接口异常，不按无字幕处理，已记录告警。" if alert else ""), link
    for alias in (ref["key"], ref["url"]):
        existing = store.find_by_url(alias)
        if existing and existing.get("notion_page_id"):
            if notion.page_alive(existing["notion_page_id"]):
                return _already_saved(msg, url, existing, note_text)
            store.forget_url(alias)
    return video.handle(msg, url, ref, note_text, say, update_id)


def _handle_url(msg: dict, url: str, note_text: str, say=None,
                update_id: int | None = None) -> tuple[str, str]:
    say = say or (lambda _t: None)
    # Share structure recognition is deferred. Raw updates stay intact, but
    # link supplements never become thoughts or model input in this policy.
    note_text = ""
    explicit_relations.remember_reply(msg)

    if chat_share.is_share(url):
        return chat_notes.handle(msg, url, note_text, say)

    # Someone already sent this link and it is parked waiting for the model.
    # `update_id` is what keeps this from tripping on itself: without it, this
    # update's own Cloud Tasks retry would find the entry it wrote a moment ago
    # and answer "already queued" forever.
    owner = store.pending_url_owner(url)
    if owner is not None and owner != update_id:
        return "⏸ 这条链接已经在队列里等 LLM 请求重试了，每 15 分钟自动检查。", ""

    existing = store.find_by_url(url)
    if existing and existing.get("notion_page_id") and \
            not notion.page_alive(existing["notion_page_id"]):
        # The user deleted the page in Notion. The index outlived it, so drop
        # the index and treat this as a fresh save rather than insisting the
        # material is still there.
        log.info("dedupe entry points at a deleted page, forgetting %s", url)
        store.forget_url(url)
        existing = None

    if existing and existing.get("notion_page_id"):
        return _already_saved(msg, url, existing, note_text)

    if extract.guess_platform(url) in VIDEO_PLATFORMS:
        return _handle_video(msg, url, note_text, say, update_id)

    # Resume the frozen source rather than repeating successful downloads/OCR.
    image_job = store.get_image_job(url) if config.GCP_PROJECT else None
    same_message = image_job and image_job.get('msg', {}).get('message_id') == msg.get('message_id') and image_job.get('msg', {}).get('chat', {}).get('id') == msg.get('chat', {}).get('id')
    if same_message and image_job.get('snapshot_uri') and image_job.get('status') != 'done':
        from . import image_batches
        return image_batches.handle(msg, url, None, image_job['platform'], '', say, image_job.get('update_id'))
    if image_job and image_job.get('snapshot_uri') and image_job.get('status') != 'done':
        return f'⏳ 这条资料正在分批处理，已完成 {image_job.get("completed", 0)} 张。', ''

    platform = extract.guess_platform(url)
    archived = ""
    say(f"⏳ 正在抓取 {platform} 正文…")

    # A paper is not a web page. Scraping arxiv.org/abs put arXivLabs,
    # "Bibliographic and Citation Tools" and the rest of the chrome into 正文;
    # the API hands over the abstract and the bibliography and nothing else.
    paper = extract.arxiv_id(url)
    doi = "" if paper else extract.doi_from_url(url)
    if paper or doi:
        say("⏳ 正在取论文信息…")
        return _handle_paper(msg, url, paper, doi, note_text, say)

    art = fetch_for(url, platform, say)
    if not art["ok"]:
        return _stage(msg, url, art["error"], note_text, f"{platform} 正文")

    description_only = _is_xhs_video(platform, art)
    if description_only and (error := _video_description_error(art)):
        return _stage(msg, url, error, "", "小红书视频简介")

    candidate = user_note(note_text, art['title'], art['text'])
    if candidate:
        _save_link_thought(msg, url, candidate)
    note_text = ''

    if art.get("paper_project"):
        say("⏳ 识别到论文项目页，正在核对论文和摘要…")
        meta = paper_project.resolve(art["paper_project"])
        if not meta.get("not_project"):
            return _handle_project_paper(msg, url, art, meta, note_text, say)

    if platform == '知乎' and 'comments' not in art:
        say('⏳ 正文已取得，正在采集高赞评论、补充与纠错…')
        art['comments'] = comments.capture(art.get('resolved_url') or url, platform)

    if not description_only and len(art.get('images') or []) > 4:
        from . import image_batches
        return image_batches.handle(msg, url, art, platform, note_text, say, update_id)

    archived = storage.archive_text(art["text"], f"{store.url_hash(url)}.txt")

    blobs, stored, unreadable = ([], [], []) if description_only else _read_url_images(art, url, say)
    if art.get('capture') and (len(stored) != len(art.get('images') or []) or not all(stored)):
        return _stage(msg, url, '备用读取的图片原件未全部归档，保留待重试',
                      note_text, f"{platform}图片", material_title=art['title'], material_text=art['text'])
    if unreadable:
        return _stage(msg, url, f"{len(unreadable)} 张图片未处理完整（下载、解码或大小限制），需重试",
                      note_text, f"{platform}图片", material_title=art['title'], material_text=art['text'])
    # Four images can exceed the gateway's request budget just as a larger
    # album can. Route by encoded bytes as well as the earlier image count.
    if blobs:
        from . import image_batches
        if image_batches.request_size(blobs) > image_batches.MAX_REQUEST_BYTES:
            return image_batches.handle(msg, url, art, platform, note_text, say, update_id)
    web_summary = platform in {"网页", "微信", "小红书", "知乎"}
    say("⏳ 正在读图并整理…" if blobs else "⏳ 正文拿到了，正在整理中文概览和要点…")
    en, en_err = enrich.enrich(art["title"], art["text"],
                               _video_description_hint(url) if description_only else _web_hint(url, platform), images=blobs,
                               note=user_note(note_text, art['title'], art['text']),
                               **({'comments': art['comments'], 'disable_thinking': True} if (art.get('comments') or {}).get('selected') else {}))
    if web_summary and en_err:
        return _stage(msg, url, en_err, note_text, f"{platform}中文总结", material_title=art['title'], material_text=art['text'])
    title = art["title"].strip() or en.title
    if description_only:
        en.material_type = "视频"
    processing = "完成" if not en_err else "需处理"
    if en_err:
        # The fallback Enrichment says 其他, which is a worse guess than the
        # platform itself makes -- a 小红书 link is a 帖子 whether or not
        # the model managed to answer.
        en.material_type = _type_for(platform)

    sources = [f"原始链接：{url}", f"收集时间：{_now()}"]
    sources.extend(_capture_source_lines(art))
    if description_only:
        sources.append("采集范围：发布者简介；未下载或归档视频文件，未解析画面或音轨")
    if archived:
        sources.append(f"原文归档：{archived}")

    extra: list[dict] = []
    if blobs:
        extra = _image_summary_blocks(en, len(blobs))
        sources.extend(_image_sources(blobs, stored))

    if en_err:
        sources.append(f"⚠️ {en_err}")

    say("⏳ 正在写入 Notion…")
    page = _create_material(
        title=title, material_type=en.material_type or "网页",
        platform=platform, url=url, topics=en.topics,
        messages=[msg],
        processing=processing,
        art={"fallback_category": en.fallback_category,
             "candidates": ({"shares": [art["thumbnail"]] if art.get("thumbnail") else [], "body": []}
                            if description_only else art.get("art_candidates") or {
            "shares": [art["thumbnail"]] if art.get("thumbnail") else [],
            "body": art.get("images") or []})},
        blocks=(_video_description_blocks(en, art, sources) if description_only else
                _web_blocks(url, en, sources, art["text"], len(blobs), art.get('comments')) if web_summary else
                _body_blocks(en.overview, art["text"], "原始正文", sources, extra=extra)),
    )
    _claim_staged(url, page["id"])

    note = user_note(note_text, en.title, art["text"])
    if note and not en.note_is_yours:
        # The keyword list only catches templates someone thought to add. This
        # catches the rest -- including text that merely echoes the article,
        # which no word list can recognise.
        log.info("dropping share boilerplate the model recognised: %r", note[:60])
        note = ""
    if note:
        t = _save_link_thought(msg, url, note, related=[page["id"]])
        return _success_text("✅ 已整理，并把你的想法接上了", title, en.overview, tag_review=en.tag_review,
                             source_url=url, platform=platform), t["url"]
    return _success_text("✅ 已收藏视频简介（未解析画面或音轨）" if description_only else "✅ 已整理", title, en.overview, tag_review=en.tag_review,
                         source_url=url, platform=platform), page["url"]


def _handle_project_paper(msg: dict, url: str, art: dict, meta: dict,
                          note_text: str, say) -> tuple[str, str]:
    if not meta["ok"]:
        return _stage(msg, url, meta["error"], note_text, "论文项目页摘要")
    archived = storage.archive_text(art["text"], f"{store.url_hash(url)}.txt")
    abstract_archive = storage.archive_text(meta["text"], f"{store.url_hash(url)}-abstract.txt")
    say("⏳ 正在总结项目页并翻译完整摘要…")
    title = art["paper_project"]["title"]
    en, error = enrich.enrich(title, art["text"], _web_hint(url, "网页"),
                             note=user_note(note_text), paper_abstract=meta["text"])
    if error:
        return _stage(msg, url, error, note_text, "论文项目页总结与摘要翻译")
    sources = [f"原始链接：{url}", f"论文链接：{art['paper_project']['paper_url']}",
               f"标识：{meta['ident']}", f"收集时间：{_now()}",
               f"摘要来源：{meta['abstract_source']}"]
    if archived:
        sources.append("项目页原文归档：" + archived)
    if abstract_archive:
        sources.append("摘要原文归档：" + abstract_archive)
    if meta.get("citations") is not None:
        sources.append(f"引用数：{meta['citations']}（{_now()} 查得）")
    page = _create_material(
        title=title, material_type="论文", platform="论文", url=url,
        topics=en.topics, processing="完成", messages=[msg],
        # Project figures are already available; avoid fetching a large PDF
        # just for decoration and retain the project-page source link.
        art={"source_kind": "paper", "fallback_category": "paper",
             "candidates": art.get("art_candidates") or {}},
        blocks=_project_blocks(url, en, meta, sources),
        extra_props=notion.paper_props(meta["authors"], meta["published"],
                                      meta["journal_ref"], meta.get("citations")))
    _claim_staged(url, page["id"])
    note = user_note(note_text, title, art["text"])
    if note and en.note_is_yours:
        t = _save_link_thought(msg, url, note, related=[page["id"]])
        return _success_text("✅ 已整理，并把你的想法接上了", title, en.overview, tag_review=en.tag_review,
                             source_url=url, platform="论文"), t["url"]
    return _success_text("✅ 已整理", title, en.overview, tag_review=en.tag_review,
                         source_url=url, platform="论文"), page["url"]


def _handle_paper(msg: dict, url: str, paper: str, doi: str, note_text: str,
                  say) -> tuple[str, str]:
    """A paper page: the link, the bibliography, an overview, the abstract.

    Two structured sources, because neither is complete on its own. arXiv has
    the abstract but never a conference; Crossref has the conference (and an
    unmetered citation count) but publishers frequently deposit no abstract.
    Semantic Scholar fills the remaining holes when it is not rate limiting us.

    The abstract remains the admission condition. A public PDF is optional
    artwork and an archived original, never a prerequisite for saving it.
    """
    meta = extract.fetch_arxiv(paper) if paper else extract.fetch_crossref(doi)
    ident = f"arXiv:{paper}" if paper else f"DOI:{doi}"
    if not meta["ok"]:
        # Same rule as any other source we could not read: not in Notion yet.
        return _stage(msg, url, meta["error"], note_text, "论文信息")

    candidate = user_note(note_text, meta['title'], meta['text'])
    if candidate:
        _save_link_thought(msg, url, candidate)
    note_text = ''

    scholar = extract.semantic_scholar(ident)
    abstract = meta["text"] or scholar.get("abstract", "")
    venue = meta["journal_ref"] or scholar.get("venue") or ""
    citations = meta.get("citations")
    if citations is None:
        citations = scholar.get("citations")

    if not abstract.strip():
        # The abstract is what a paper page holds. Without it the page is a
        # bibliography card, and an overview written from the title alone is
        # the model guessing.
        return _stage(msg, url, "arXiv / Crossref / Semantic Scholar 都没有提供摘要",
                      note_text, "论文摘要")

    say("⏳ 正在整理中文概览…")
    en, en_err = enrich.enrich(meta["title"], abstract, f"论文 {ident}",
                               note=user_note(note_text), paper_abstract=abstract)
    if en_err:
        return _stage(msg, url, en_err, note_text, "论文概览与摘要翻译")

    sources = [f"原始链接：{url}", f"标识：{ident}", f"收集时间：{_now()}"]
    if meta["doi"] and not doi:
        sources.append(f"DOI：{meta['doi']}")
    if citations is not None:
        sources.append(f"引用数：{citations}（{_now()} 查得）")
    if en_err:
        sources.append(f"⚠️ {en_err}")

    pdf_data, pdf_archive = thumbnails.paper_pdf(url, meta.get("pdf_urls") or [])
    if pdf_archive:
        sources.append("论文 PDF 原件归档：" + pdf_archive)

    blocks = _body_blocks(en.overview, "", "", [])
    blocks.extend(_abstract_blocks(abstract, en.abstract_zh))
    blocks.append(notion.toggle("来源与定位", notion.bullets(sources)))
    page = _create_material(
        title=meta["title"].strip() or en.title, material_type="论文", platform="论文",
        messages=[msg],
        url=url, topics=en.topics, processing="完成" if not en_err else "需处理",
        art={"source_kind": "paper", "fallback_category": "paper",
             **({"pdf_bytes": pdf_data} if pdf_data else {})},
        blocks=blocks,
        extra_props=notion.paper_props(meta["authors"], meta["published"],
                                       venue, citations))
    _claim_staged(url, page["id"])

    note = user_note(note_text, en.title, abstract)
    if note and not en.note_is_yours:
        note = ""
    title = meta["title"].strip() or en.title
    if note:
        t = _save_link_thought(msg, url, note, related=[page["id"]])
        return _success_text("✅ 已整理，并把你的想法接上了", title, en.overview, tag_review=en.tag_review,
                             source_url=url, platform="论文"), t["url"]
    return _success_text("✅ 已整理", title, en.overview, tag_review=en.tag_review,
                         source_url=url, platform="论文"), page["url"]


_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


def _handle_note_start(msg: dict, payload: str) -> tuple[str, str]:
    """A 「记下想法」 tap from a material page in Notion.

    The link only says which material; the note itself is the next message. So
    arm it and get out of the way -- the user tapped the link in order to start
    typing, not to answer a prompt.
    """
    raw = payload[len(telegram.NOTE_START_PREFIX):]
    if not _HEX32.match(raw):
        return "这个链接不认识。直接把想法发过来也一样。", ""
    page = notion.get_page(str(uuid.UUID(raw)))
    if page is None:
        return "那份资料已经不在了。直接把想法发过来吧。", ""
    title = notion.prop_text(page, "标题") or "那份资料"
    chat_id = msg["chat"]["id"]
    token = store.set_pending_note(chat_id, page["id"])
    markup = telegram.keyboard([[('取消', 'nc:' + token)]])
    markup['inline_keyboard'].insert(0, [
        {'text': '打开 Notion', 'url': notion.page_url(page)}])
    try:
        telegram.send(chat_id,
                      f"✍️ 下一条消息会记成《{telegram.esc(title)}》的笔记。",
                      reply_to=msg['message_id'], markup=markup, parse_mode='HTML')
    except Exception:
        # A failed prompt must not leave an invisible mode behind.
        store.cancel_pending_note(chat_id, token)
        raise
    return "", ""


def _handle_thought(msg: dict, text: str) -> tuple[str, str]:
    related = explicit_relations.note_targets(msg)
    # Always consume, so a tap the user walked away from cannot survive past
    # this message either way.
    armed = store.take_pending_note(msg["chat"]["id"])
    if not related and armed:
        related.append(armed)

    title = re.sub(r"\s+", " ", text.strip())[:60]
    blocks = notion.paragraphs(text.strip())
    page = notion.create_thought(
        title=title,
        note_type="阅读笔记" if related else "随想",
        topics=[], related=related, blocks=blocks,
    )
    container, body_ids = notion.body_location(page)
    store.put_record(msg["chat"]["id"], msg["message_id"], kind="thought",
                     notion_page_id=page["id"], notion_url=page["url"],
                     body_block_ids=body_ids, body_parent_id=container, text=text)
    explicit_relations.queue_note(msg, page['id'], related)
    return ("✅ 已记下，并关联到那份资料" if related else "✅ 已记下"), page["url"]


def _handle_photo(msg: dict, say=None) -> tuple[str, str]:
    say = say or (lambda _t: None)
    photo = max(msg["photo"], key=lambda p: p.get("file_size") or 0)
    say("⏳ 正在下载图片…")
    data, path = telegram.download(photo["file_id"])
    name = f"{store.record_id(msg['chat']['id'], msg['message_id'])}.jpg".replace(":", "_")
    archived = storage.archive(data, name, "image/jpeg")

    say("⏳ 正在做 OCR…")
    ocr = extract.ocr_image(data)
    say("⏳ 正在整理…")
    caption = (msg.get("caption") or "").strip()
    en, en_err = enrich.enrich(caption, ocr["text"], "一张图片的 OCR 文本")

    extra: list[dict] = []
    upload_id = notion.upload_file(data, name, "image/jpeg")
    if upload_id:
        extra = [notion.heading("原图"), notion.image_block(upload_id)]

    ocr_blocks: list[dict] = []
    if ocr["text"]:
        ocr_blocks = [notion.heading("OCR 原文"),
                      notion.callout("机器识别结果，原样保留，未人工校对。", "🤖",
                                     "blue_background")]
        ocr_blocks.extend(notion.code(ocr["text"]))

    sources = [f"收集时间：{_now()}", "OCR 引擎：Google Cloud Vision"]
    if archived:
        sources.append(f"原图归档：{archived}")
    if not upload_id:
        sources.append("⚠️ 原图未能上传到 Notion，只在 GCS 归档中")
    for err in (ocr.get("error"), en_err):
        if err:
            sources.append(f"⚠️ {err}")

    blocks = _body_blocks(en.overview, "", "", sources, extra=extra + ocr_blocks)
    page = _create_material(
        title=en.title or "图片", material_type="图片", platform="网页",
        messages=[msg],
        url=None, topics=en.topics,
        processing="完成" if ocr["ok"] else "需处理",
        reading="待读", blocks=blocks,
        art={"image_bytes": data, "source_kind": "image"},
    )
    if not ocr["ok"]:
        return f"⚠️ 图片存好了，但没识别出文字\n{telegram.esc(ocr.get('error', ''))}", page["url"]
    return _success_text("✅ 已整理", en.title, en.overview, tag_review=en.tag_review), page["url"]


def _handle_photo_group(msg: dict, members: list[dict], say=None) -> tuple[str, str]:
    """One album -> one page.

    Telegram delivers an album as N separate updates, so this used to build N
    materials: five screenshots of one 公众号 article became five unrelated
    pages, each with its own OCR and its own overview of one fifth of the
    argument. README offers screenshots as the way round 公众号 being
    unreachable, which only works if they land as one material.

    The pictures go through the same single multimodal call the 图文帖 path
    uses rather than per-image Cloud Vision: judging which pictures carry
    information, transcribing those and writing one overview is a judgement
    about the set, and it can only be made by something that sees all of them
    at once.
    """
    say = say or (lambda _t: None)
    chat_id = msg["chat"]["id"]
    say(f"⏳ 这组有 {len(members)} 张图，正在下载…")

    blobs: list[tuple[bytes, str]] = []
    archived: list[str] = []
    names: list[str] = []
    oversized: list[int] = []
    total = 0
    first_original = None
    for i, m in enumerate(members[:extract.MAX_IMAGES]):
        data, _ = telegram.download(m["file_id"])
        if i == 0:
            first_original = data
        name = f"{store.record_id(chat_id, m['message_id'])}.jpg".replace(":", "_")
        # Archived before any size check: an original is kept whether or not we
        # were able to read it (plan section 17).
        archived.append(storage.archive(data, name, "image/jpeg"))
        if len(data) > extract.MAX_IMAGE_BYTES or \
                total + len(data) > extract.MAX_TOTAL_IMAGE_BYTES:
            oversized.append(i)
            continue
        total += len(data)
        blobs.append((data, "image/jpeg"))
        names.append(name)

    caption = next((m.get("caption") or "" for m in members
                    if m.get("caption") and not extract.find_urls(m["caption"])), "")
    say("⏳ 正在读图并整理…")
    en, en_err = enrich.enrich(caption, "", f"一组 {len(members)} 张图片",
                               images=blobs)

    extra: list[dict] = []
    uploaded = 0
    for i, ((data, mime), name) in enumerate(zip(blobs, names)):
        upload_id = notion.upload_file(data, name, mime)
        if upload_id:
            if not uploaded:
                extra.append(notion.heading("原图"))
            uploaded += 1
            extra.append(notion.image_block(upload_id))

    reads = {im.index: im for im in en.images}
    kept = [(i, reads[i]) for i in range(len(blobs))
            if i in reads and reads[i].informative and reads[i].text.strip()]
    skipped = [(i, reads[i]) for i in range(len(blobs))
               if i in reads and not reads[i].informative]
    if kept:
        extra.append(notion.heading("图片文字"))
        extra.append(notion.callout(
            "机器转写，原样保留。装饰性图片未转写，原件仍在归档中。", "🤖",
            "blue_background"))
        for i, im in kept:
            extra.append(notion.heading(f"图 {i + 1}"))
            extra.extend(notion.paragraphs(im.text))
    if skipped:
        extra.append(notion.toggle(
            f"跳过了 {len(skipped)} 张装饰性图片",
            notion.bullets([f"图 {i + 1}：{im.note or '无实质信息'}"
                            for i, im in skipped])))

    sources = [f"收集时间：{_now()}",
               f"图片：{len(members)} 张，已归档 {sum(1 for a in archived if a)} 张"]
    if len(members) > extract.MAX_IMAGES:
        sources.append(f"⚠️ 单条上限 {extract.MAX_IMAGES} 张，"
                       f"其余 {len(members) - extract.MAX_IMAGES} 张未识别")
    if oversized:
        sources.append(f"⚠️ {len(oversized)} 张图太大，未送去识别；原件仍在归档中")
    if uploaded < len(blobs):
        sources.append(f"⚠️ {len(blobs) - uploaded} 张原图未能上传到 Notion，"
                       "只在 GCS 归档中")
    if en_err:
        sources.append(f"⚠️ {en_err}")

    blocks = _body_blocks(en.overview, "", "", sources, extra=extra)
    page = _create_material(
        title=en.title or f"一组图片（{len(members)} 张）", material_type="图片",
        messages=[{"chat": {"id": chat_id}, "message_id": m["message_id"]} for m in members],
        platform="网页", url=None, topics=en.topics,
        processing="完成" if kept else "需处理", reading="待读", blocks=blocks,
        art={"image_bytes": first_original, "source_kind": "image"},
    )
    # Every message in the album points at the page, so replying to any of the
    # screenshots attaches a thought to the material rather than to nothing.
    if not kept:
        return (f"⚠️ {len(members)} 张图存好了，但没识别出实质内容"
                + (f"\n{telegram.esc(en_err)}" if en_err else "")), page["url"]
    return _success_text(f"✅ 已把 {len(members)} 张图整理成一页",
                         en.title, en.overview, tag_review=en.tag_review), page["url"]


def _handle_document(msg: dict, say=None) -> tuple[str, str]:
    say = say or (lambda _t: None)
    doc = msg["document"]
    name = doc.get("file_name") or "file"
    mime = doc.get("mime_type") or "application/octet-stream"
    is_pdf = mime == "application/pdf" or name.lower().endswith(".pdf")
    if is_pdf:
        mime = "application/pdf"
    say(f"⏳ 正在下载 {name}…")
    data, _ = telegram.download(doc["file_id"])
    archived = storage.archive(data, f"{store.url_hash(name + str(doc['file_id']))}-{name}", mime)

    scan_pages: list = []
    scanned_total = 0
    if is_pdf:
        parsed = extract.pdf_text(data)
        mtype = "论文"
        if not parsed["ok"]:
            # No text layer: it is a scan. Render the pages and let the
            # multimodal model read them, rather than adding a dedicated OCR --
            # the summarising call has to happen anyway, so the pages cost
            # image tokens and nothing else.
            say("⏳ 这个 PDF 没有文字层，正在按扫描件识别…")
            scan_pages, scanned_total = extract.pdf_page_images(data)
    else:
        try:
            parsed = {"ok": True, "title": name, "text": data.decode("utf-8"),
                      "pages": 0, "error": ""}
        except UnicodeDecodeError:
            parsed = {"ok": False, "title": name, "text": "", "pages": 0,
                      "error": f"不支持的文件类型：{mime}"}
        mtype = "其他"

    caption = (msg.get("caption") or "").strip()
    say("⏳ 正在整理…")
    summary_text, summary_scope = (enrich.pdf_summary_input(parsed["text"]) if is_pdf else
                                  (parsed["text"], ""))
    if scan_pages:
        summary_scope = f"总结范围：扫描件前 {len(scan_pages)} 页（共 {scanned_total} 页）"
    hint = (enrich.PDF_SUMMARY_HINT + f"文件：{name}\n{summary_scope}" if is_pdf else f"文件：{name}")
    en, en_err = enrich.enrich(parsed["title"] or name, summary_text,
                               hint, images=scan_pages,
                               note=user_note(caption))
    if scan_pages and en.images:
        # The transcription is the only text this document has. It goes in as
        # 图片文字, the same section OCR'd posts use, so card_metadata counts it
        # and the reading estimate is not silently zero.
        parsed["text"] = "\n\n".join(im.text for im in en.images if im.text)
        parsed["ok"] = bool(parsed["text"] or (en.overview and any(im.informative for im in en.images)))
        if parsed["ok"]:
            parsed["error"] = ""

    extra: list[dict] = []
    upload_id = notion.upload_file(data, name, mime)
    if upload_id and mime == "application/pdf":
        extra = [notion.heading("原件"), notion.pdf_block(upload_id)]
    if is_pdf and not archived and not upload_id:
        raise RuntimeError("PDF 原件尚未保存成功，保留原消息待重试")

    sources = [f"文件名：{name}", f"收集时间：{_now()}"]
    if summary_scope:
        sources.append(summary_scope)
    if is_pdf and not upload_id:
        sources.append("⚠️ Notion 原件附件未上传成功；原件保存在归档中")
    if parsed.get("pages") or scanned_total:
        sources.append(f"页数：{parsed.get('pages') or scanned_total}")
    if scan_pages:
        sources.append(
            f"扫描件，已识别前 {len(scan_pages)} 页" +
            (f"（共 {scanned_total} 页，其余未识别）"
             if scanned_total > len(scan_pages) else ""))
    if archived:
        sources.append(f"原件归档：{archived}")
    for err in (parsed.get("error"), en_err):
        if err:
            sources.append(f"⚠️ {err}")

    page = _create_material(
        title=en.title or name, material_type=en.material_type or mtype,
        messages=[msg],
        platform="论文" if mtype == "论文" else "网页",
        url=None, topics=en.topics,
        processing="完成" if parsed["ok"] and not en_err else "需处理",
        art={"source_kind": "document", "fallback_category": en.fallback_category,
             **({"pdf_bytes": data} if mtype == "论文" else {})},
        blocks=([*_body_blocks(en.overview, "", "", []),
                 *([notion.heading("中文要点"), *notion.bullets(en.key_points)] if en.key_points else []),
                 *extra, notion.toggle("来源与定位", notion.bullets(sources))] if is_pdf else
                _body_blocks(en.overview, parsed["text"], "提取正文", sources)),
    )
    # A forwarded PDF carries share boilerplate in its caption just like a link
    # does; this path used to file all of it as a thought, unfiltered.
    note = user_note(caption, en.title, parsed["text"])
    if note and not en.note_is_yours:
        log.info("dropping caption boilerplate: %r", note[:60])
        note = ""
    if note:
        _save_link_thought(msg, '', note)
    if not parsed["ok"]:
        return f"⚠️ 文件存好了，但正文没提取出来\n{telegram.esc(parsed['error'])}", page["url"]
    if en_err:
        return f"⚠️ 原件已保存，中文总结尚未完成\n{telegram.esc(en_err)}", page["url"]
    return _success_text("✅ 已整理", en.title, en.overview, tag_review=en.tag_review), page["url"]


def _handle_edit(msg: dict, text: str) -> tuple[str, str]:
    """User corrected a dictation error (plan section 14): use the latest text."""
    rec = store.get_record(msg["chat"]["id"], msg["message_id"])
    if not rec:
        return "", ""   # never saw the original; nothing to correct
    if extract.find_urls(text):
        return "链接消息的附带文字已忽略，已有笔记保持原样。", rec.get('notion_url', '')
    if rec.get("kind") == "chat_share":
        return "分享会话第一版不自动覆盖已有笔记；请直接在 Notion 编辑补充。", rec.get("notion_url", "")
    attached = rec.get("link_thought")
    if attached:
        note = text.strip() if attached.get('preserve_full_message') else extract.without_first_url(text).strip()
        # Resolve live block IDs: a staged note can have acquired a shared body
        # since the last edit. The material's mapping remains untouched.
        container, old_ids = notion.body_location({"id": attached["id"]})
        ids = notion.replace_paragraphs(container, old_ids, note)
        notion.update_properties(attached["id"], {
            "标题": {"title": [{"text": {"content": re.sub(r"\s+", " ", extract.without_first_url(note, keep_label=False)).strip()[:200]
                                          or "（想法已清空）"}}]}})
        fields = {"link_thought": {**attached, "body_parent_id": container,
                                  "body_block_ids": ids, "text": note}}
        if rec.get("kind") == "thought" and rec.get("notion_page_id") == attached["id"]:
            fields.update(body_parent_id=container, body_block_ids=ids, text=note)
        store.put_record(msg["chat"]["id"], msg["message_id"], **fields)
        return "✏️ 已更新为最新版本", attached.get("url", "")
    if rec.get("waiting_update_id") is not None:
        uid = rec["waiting_update_id"]
        pending = store.get_update(uid) or {}
        if pending.get("status") == "waiting_llm":
            payload = pending.get("payload") or {}
            original = pending.get("latest_message") or payload.get("message") or payload.get("edited_message") or {}
            source = first_url(original)
            if source:
                note = extract.without_first_url(text).strip()
                store.set_latest_message(uid, {**original, "text": (source + "\n" + note).strip()})
                return "✏️ 已更新附带想法，资料整理时会使用最新版本。", ""
    if not rec.get("notion_page_id"):
        return "", ""
    page_id = rec["notion_page_id"]
    if rec.get("kind") != "thought":
        return "", ""   # editing a link's caption does not rewrite a material
    # Records written before notes were mirrored have no container; their body
    # is a child of the page, which is exactly the old behaviour.
    container = rec.get("body_parent_id") or page_id
    ids = notion.replace_paragraphs(container, rec.get("body_block_ids", []), text.strip())
    notion.update_properties(page_id, {
        "标题": {"title": [{"text": {"content": re.sub(r'\s+', ' ', text.strip())[:200]}}]}
    })
    store.put_record(msg["chat"]["id"], msg["message_id"],
                     body_block_ids=ids, body_parent_id=container, text=text)
    return "✏️ 已更新为最新版本", rec.get("notion_url", "")


# ---------- entry point ----------

# One retry pass must finish inside Cloud Run's 300s request ceiling, so it
# works until the budget is spent and reports what is left for the next run.
RETRY_BUDGET_SECONDS = 210


def _retry_one(page: dict, say) -> tuple[bool, str]:
    """Re-run extraction and tidying for one material.

    Only the machine-owned fields are rewritten. The overview is appended as a
    new section because the failed run never wrote one -- nothing is overwritten,
    least of all anything the user typed.
    """
    page_id = page["id"]
    title = notion.prop_text(page, "标题") or "(无标题)"
    url = notion.prop_text(page, "原始链接")
    if not url:
        return False, f"{telegram.esc(title)}：没有原始链接，跳过"
    if extract.guess_platform(url) in VIDEO_PLATFORMS:
        from . import video
        return video.retry_page(url, say)

    art = fetch_for(url, say=say)
    if not art["ok"]:
        return False, f"{telegram.esc(title)}：{telegram.esc(art['error'] or '正文仍然抓不到')}"

    platform = extract.guess_platform(url)
    description_only = _is_xhs_video(platform, art)
    if description_only and (error := _video_description_error(art)):
        return False, f"{telegram.esc(title)}：{error}"
    if platform == '知乎':
        art['comments'] = comments.capture(art.get('resolved_url') or url, platform)
    blobs, stored, missing = ([], [], []) if description_only else _read_url_images(art, url, say)
    if art.get('capture') and (len(stored) != len(art.get('images') or []) or not all(stored)):
        return False, f"{telegram.esc(title)}：备用读取的图片原件未全部归档，需重试"
    if missing:
        return False, f"{telegram.esc(title)}：{len(missing)} 张图片未处理完整，需重试"
    image_args = {"images": blobs} if blobs else {}
    if (art.get('comments') or {}).get('selected'):
        image_args.update(comments=art['comments'], disable_thinking=True)
    en, en_err = enrich.enrich(art["title"] or title, art["text"],
                               _video_description_hint(url) if description_only else _web_hint(url, platform), **image_args)
    if en_err:
        return False, f"{telegram.esc(title)}：{telegram.esc(en_err)}"

    if description_only:
        en.material_type = "视频"
        notion.append_blocks(page_id, _video_description_blocks(en, art, [f"原始链接：{url}", f"收集时间：{_now()}"]))
    elif platform in {"网页", "微信", "小红书", "知乎"}:
        notion.append_blocks(page_id, _web_blocks(url, en, [*_image_sources(blobs, stored), *_capture_source_lines(art)],
                                               art["text"] if blobs or art.get('capture') else "", len(blobs), art.get('comments')))
    elif en.overview:
        notion.append_blocks(page_id, [
            notion.heading("中文概览（后补）"),
            *notion.paragraphs(en.overview),
        ])
    original_title = art["title"].strip() or title
    notion.refresh_machine_fields(
        page_id, title=original_title,
        material_type=en.material_type or "其他",
        topics=en.topics, processing="完成", overview=en.overview, existing_page=page)
    review_text = _tag_review_text(en.tag_review)
    return True, telegram.esc(original_title) + ('\n' + review_text if review_text else '')


def _retry_staged(row: dict, say, *, image_job=False) -> tuple[bool, str]:
    """Send a staged link through the normal ingestion path again.

    Going through _handle_url rather than a copy of it means a success builds
    exactly the page a first-time success would, and a failure re-stages with
    the attempt count bumped.
    """
    url = row["url"]
    text = ''
    if row.get("ingest_kind") == "chat_share" or chat_share.is_share(url):
        return chat_notes.retry(row, say)
    if row.get("page_id"):
        # Already in Notion; only the relation to its thoughts is outstanding.
        _claim_staged(url, row["page_id"])
    elif image_job:
        from . import image_batches
        result = image_batches.resume(url, notify=False, manual=True, say=say)
        if result.get('busy'):
            return False, f"{telegram.esc(url)}：图片任务正在处理"
    else:
        msg = {"chat": {"id": row.get("chat_id", 0)},
               "message_id": row.get("message_id", 0)}
        raw_note = (row.get('attached_note') or {}).get('raw', '') if not row.get('thought_ids') else ''
        text, _ = _handle_url(msg, url, raw_note, say)
    left = store.get_staged(url)
    if left:
        return False, (f"{telegram.esc(url)} \n  {telegram.esc(_short_error(left.get('error', '')) or '仍然抓不到')}")
    return True, (telegram.esc(url) + '\n' + text if '标签待确认：' in text else url)


@work_budget.limited(RETRY_BUDGET_SECONDS)
def _handle_retry(say, *, automatic: bool = False) -> tuple[str, str]:
    owner = store.claim_retry()
    if not owner:
        return "⏸ 已有重试任务在运行，本次跳过", ""
    try:
        return _retry_pending(say, automatic=automatic)
    finally:
        store.release_retry(owner)


def _retry_pending(say, *, automatic: bool = False) -> tuple[str, str]:
    jobs = {store.url_hash(job['url']): job for job in store.pending_image_jobs()
            if job.get('snapshot_uri')} if config.GCP_PROJECT else {}
    staged = store.staged_urls()
    if automatic:
        # The image worker has its own retry budget and continuation schedule.
        staged = [r for r in staged if store.url_hash(r['url']) not in jobs]
    job_pages = {(job.get('page') or {}).get('id') for job in jobs.values()}
    pending = [("staged", r) for r in staged] + \
        [("page", p) for p in notion.find_materials(("需处理", "等待采集")) if p["id"] not in job_pages]
    if not pending:
        parked = len(store.waiting_for_llm())
        if parked:
            return f"✅ 没有需要重跑的材料。另有 {parked} 条还没建页，等待 LLM 请求重试（每 15 分钟自动检查）。", ""
        return "✅ 没有需要重跑的材料", ""

    started = time.monotonic()
    done, failed, notes = 0, 0, []
    for i, (kind, item) in enumerate(pending, 1):
        if time.monotonic() - started > RETRY_BUDGET_SECONDS or work_budget.remaining() < max(120, config.LLM_TIMEOUT+60):
            followup = "下次自动重试继续" if automatic else "再发一次 /retry"
            notes.append(f"⏸ 时间用完了，还剩 {len(pending) - i + 1} 条，{followup}")
            break
        say(f"⏳ 重跑 {i}/{len(pending)}…")
        try:
            if kind == "staged":
                if store.url_hash(item['url']) in jobs:
                    ok, detail = _retry_staged(item, say, image_job=True)
                else:
                    ok, detail = _retry_staged(item, say)
            else:
                ok, detail = _retry_one(item, say)
        except enrich.EndpointUnavailable as e:
            # No point walking the rest of the backlog into the same wall.
            notes.append(f"⏸ LLM 请求未完成，剩下 {len(pending) - i + 1} 条没跑："
                         f"{telegram.esc(e)}")
            break
        except Exception as e:
            label = item["url"] if kind == "staged" else notion.prop_text(item, "标题")
            ok, detail = False, f"{telegram.esc(label)}：{telegram.esc(e)}"
        if ok:
            done += 1
            if '标签待确认：' in detail:
                notes.append('· ' + detail)
        else:
            failed += 1
            notes.append(f"· {detail}")

    lines = [f"重跑完成：{done} 条成功，{failed} 条仍未完成"]
    if staged:
        lines.append(f"其中暂存区链接 {len(staged)} 条，成功的已入库")
    shown = 0
    for detail in notes[:8]:
        if len('\n'.join([*lines, detail])) > 3500:
            break
        lines.append(detail)
        shown += 1
    if shown < len(notes):
        lines.append(f'另有 {len(notes) - shown} 项结果未展开，批量回复已达长度上限。')
    parked = len(store.waiting_for_llm())
    if parked:
        lines.append(f"另有 {parked} 条还没建页，等待 LLM 请求重试（每 15 分钟自动检查）。")
    return "\n".join(lines), ""


LLM_HELP = (
    "/llm                查看当前使用的模型\n"
    "/llm use 「名字」      切换到已登记的服务商\n"
    "/llm add 「名字」「base_url」「模型」「key」   登记一个服务商\n"
    "/llm model 「模型」    只换模型，不用重填 key\n"
    "/llm rm 「名字」       删除一个服务商\n"
    "/llm test           用当前配置试跑一次\n\n"
    "名字 default 表示部署时配置的那个，永远可以切回去。"
)


PAGE_SIZE = 8   # buttons per page; Telegram keyboards get unusable past this


def _pick_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}_{message_id}"


def provider_keyboard(names: list[str], active_name: str) -> dict:
    rows = [[(("✅ " if n == active_name else "") + n, f"lp:{i}")]
            for i, n in enumerate(names)]
    rows.append([("换模型", "lmp:0"), ("试跑", "lt"), ("关闭", "lx")])
    return telegram.keyboard(rows)


def model_keyboard(models: list[str], page: int, current: str) -> dict:
    total = max(1, (len(models) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total - 1))
    start = page * PAGE_SIZE
    rows = [[(("✅ " if m == current else "") + m, f"lm:{start + i}")]
            for i, m in enumerate(models[start:start + PAGE_SIZE])]
    nav: list[tuple[str, str]] = []
    if page > 0:
        nav.append(("◀ 上一页", f"lmp:{page - 1}"))
    if page < total - 1:
        nav.append(("下一页 ▶", f"lmp:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("返回", "lb"), ("关闭", "lx")])
    return telegram.keyboard(rows)


def llm_status_text() -> str:
    """Status plus the commands buttons cannot express.

    Registering a provider needs four typed arguments, so the syntax has to be
    on screen -- nobody remembers it, and there is nowhere else to look it up
    from inside Telegram.
    """
    cur = settings.active()
    return (f"当前：{cur['name']}\n"
            f"端点 {cur['base_url'] or '（未设置）'}\n"
            f"模型 {cur['model'] or '（未设置）'}\n"
            f"key  {settings.redact_key(cur.get('api_key', ''))}\n"
            "\n切换配置、换模型、试跑都可以点下面的按钮。\n"
            "\n要打字的：\n"
            "/llm add 「名字」「base_url」「模型」「key」\n"
            "    登记新服务商并立即切过去\n"
            "/llm rm 「名字」\n"
            "    删掉一个\n"
            "/llm model 「模型名」\n"
            "    端点列不出模型时直接指定\n"
            "\n例：\n"
            "/llm add ds https://api.deepseek.com/v1 deepseek-chat sk-xxxx")


def send_provider_picker(chat_id: int, trigger_message_id: int = 0) -> None:
    names = ["default"] + sorted(settings.providers())
    sent = telegram.send(chat_id, llm_status_text(),
                         markup=provider_keyboard(names, settings.active()["name"]))
    settings.save_picks(_pick_key(chat_id, sent["message_id"]),
                        ["P"] + names, trigger_message_id)


def send_model_picker(chat_id: int, trigger_message_id: int = 0,
                      page: int = 0, edit_message_id: int = 0) -> None:
    models, err = settings.list_models()
    cur = settings.active()
    if err:
        text = f"取不到模型列表：{err}\n\n可以直接用 /llm model 「模型名」"
        markup = telegram.keyboard([[("返回", "lb"), ("关闭", "lx")]])
        models = []
    else:
        text = f"{cur['name']} 上有 {len(models)} 个模型，当前 {cur['model']}"
        markup = model_keyboard(models, page, cur["model"])
    if edit_message_id:
        telegram.edit(chat_id, edit_message_id, text, markup=markup)
        mid = edit_message_id
    else:
        mid = telegram.send(chat_id, text, markup=markup)["message_id"]
    settings.save_picks(_pick_key(chat_id, mid), ["M"] + models,
                        trigger_message_id)


def _picks(chat_id: int, message_id: int) -> tuple[str, list[str], int]:
    """Returns (kind, items, id of the /llm message that opened this picker).

    The kind is a one-character prefix, and the trigger is stored in its own
    field -- packing it into the same list would break the moment someone named
    a provider after the separator.
    """
    raw, trigger = settings.load_picks(_pick_key(chat_id, message_id))
    if not raw:
        return "", [], 0
    return raw[0], raw[1:], trigger


def handle_callback(cb: dict) -> None:
    """Button presses. Picking something clears the picker and the /llm message
    that opened it, so the chat is not left full of menu scrollback."""
    data = cb.get("data") or ""
    if data.startswith("tg:"):
        from . import tag_ui
        tag_ui.handle_callback(cb)
        return
    message = cb.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    if not chat_id or not message_id:
        telegram.answer_callback(cb["id"])
        return

    if data.startswith('nc:'):
        token = data[3:]
        if not _HEX32.fullmatch(token):
            telegram.answer_callback(cb['id'], '这个按钮已失效')
            return
        cancelled = store.cancel_pending_note(chat_id, token)
        text = ('已取消。下一条消息会照常处理。' if cancelled else
                '这条记录提示已结束或失效。')
        telegram.answer_callback(cb['id'], text)
        telegram.edit(chat_id, message_id, text, markup=telegram.keyboard([]))
        return

    kind, items, trigger = _picks(chat_id, message_id)

    def close(note: str) -> None:
        telegram.answer_callback(cb["id"], note)
        telegram.delete(chat_id, message_id)
        if trigger:
            telegram.delete(chat_id, trigger)

    if data == "lx":
        close("已关闭")
        return

    if data == "lt":
        telegram.answer_callback(cb["id"], "正在试跑…")
        cur = settings.active()
        started = time.monotonic()
        en, err = enrich.enrich("测试标题", "一段用于测试的正文，讲知识管理里的回流问题。")
        took = time.monotonic() - started
        result = (f"❌ {cur['name']} 失败（{took:.1f}s）\n{err}" if err else
                  f"✅ {cur['name']} 正常（{took:.1f}s）\n模型 {cur['model']}\n"
                  f"标题：{en.title}")
        names = ["default"] + sorted(settings.providers())
        telegram.edit(chat_id, message_id, result + "\n\n" + llm_status_text(),
                      markup=provider_keyboard(names, cur["name"]))
        settings.save_picks(_pick_key(chat_id, message_id), ["P"] + names, trigger)
        return

    if data == "lb":
        telegram.answer_callback(cb["id"])
        names = ["default"] + sorted(settings.providers())
        telegram.edit(chat_id, message_id, llm_status_text(),
                      markup=provider_keyboard(names, settings.active()["name"]))
        settings.save_picks(_pick_key(chat_id, message_id), ["P"] + names, trigger)
        return

    if data.startswith("lmp:"):
        telegram.answer_callback(cb["id"])
        send_model_picker(chat_id, trigger, page=int(data.split(":")[1]),
                          edit_message_id=message_id)
        return

    if data.startswith("lp:") and kind == "P":
        i = int(data.split(":")[1])
        name = items[i] if 0 <= i < len(items) else ""
        if name and settings.use(name):
            now = settings.active()
            close(f"已切到 {name} · {now['model']}")
            telegram.send(chat_id, f"✅ 现在用 {name}\n{now['model']} @ {now['base_url']}")
        else:
            telegram.answer_callback(cb["id"], "切换失败")
        return

    if data.startswith("lm:") and kind == "M":
        i = int(data.split(":")[1])
        model = items[i] if 0 <= i < len(items) else ""
        if not model:
            telegram.answer_callback(cb["id"], "这个选项已过期")
            return
        name = settings.set_model(model)
        if not name:
            telegram.answer_callback(cb["id"], "default 的模型在部署时写死，先 /llm add")
            return
        close(f"已换成 {model}")
        telegram.send(chat_id, f"✅ {name} 的模型已改成 {model}")
        return

    telegram.answer_callback(cb["id"], "这个按钮已过期")


def _handle_llm(args: list[str], say, chat_id: int = 0,
                trigger_message_id: int = 0) -> tuple[str, str]:
    """Switch LLM provider from chat.

    The whole point is that when the shared cluster is down, moving to a paid
    API is a message instead of a redeploy.
    """
    cur = settings.active()

    if not args or args[0] in ("help", "-h"):
        send_provider_picker(chat_id, trigger_message_id)
        return "", ""

    if args[0] == "model" and len(args) == 1:
        send_model_picker(chat_id, trigger_message_id, page=0)
        return "", ""

    cmd, rest = args[0], args[1:]

    if cmd == "use":
        if not rest:
            return "用法：/llm use 「名字」", ""
        if settings.use(rest[0]):
            now = settings.active()
            return (f"✅ 已切到 {telegram.esc(now['name'])}\n"
                    f"{telegram.esc(now['model'])} @ {telegram.esc(now['base_url'])}"), ""
        return f"没有叫 {telegram.esc(rest[0])} 的服务商。/llm 看列表", ""

    if cmd == "add":
        if len(rest) < 4:
            return "用法：/llm add 「名字」「base_url」「模型」「key」", ""
        name, base_url, model, key = rest[0], rest[1], rest[2], " ".join(rest[3:])
        settings.add(name, base_url, model, key)
        settings.use(name)
        return (f"✅ 已登记并切到 {telegram.esc(name)}\n"
                f"{telegram.esc(model)} @ {telegram.esc(base_url)}\n\n"
                "⚠️ 这条消息里有你的 API key，建议现在就删掉它 —— "
                "Telegram 的聊天记录会一直留着。"), ""

    if cmd == "model":
        if not rest:
            return "用法：/llm model 「模型名」", ""
        name = settings.set_model(rest[0])
        if not name:
            return ("default 是部署时写死的，改不了模型。\n"
                    "先用 /llm add 登记一个，或者重新部署。"), ""
        return f"✅ {telegram.esc(name)} 的模型已改成 {telegram.esc(rest[0])}", ""

    if cmd == "rm":
        if not rest:
            return "用法：/llm rm 「名字」", ""
        if settings.remove(rest[0]):
            return (f"✅ 已删除 {telegram.esc(rest[0])}，"
                    f"当前用 {telegram.esc(settings.active()['name'])}"), ""
        return f"没有叫 {telegram.esc(rest[0])} 的服务商", ""

    if cmd == "test":
        say("⏳ 正在试跑…")
        started = time.monotonic()
        try:
            en, err = enrich.enrich("测试标题", "这是一段用于测试的正文，讲的是知识管理里的回流问题。")
        except enrich.EndpointUnavailable as e:
            # Everywhere else an unreachable endpoint means "park it and stop".
            # Not here: diagnosing the endpoint is this command's whole job, and
            # letting the exception fly would answer a question about the
            # endpoint with "处理了 4 次都失败了".
            took = time.monotonic() - started
            return (f"❌ {telegram.esc(cur['name'])} 连不上（{took:.1f}s）\n"
                    f"{telegram.esc(e)}"), ""
        took = time.monotonic() - started
        if err:
            return f"❌ {telegram.esc(cur['name'])} 失败（{took:.1f}s）\n{telegram.esc(err)}", ""
        return (f"✅ {telegram.esc(cur['name'])} 正常（{took:.1f}s）\n"
                f"模型 {telegram.esc(cur['model'])}\n"
                f"标题：{telegram.esc(en.title)}\n概览：{telegram.esc(en.overview[:120])}"), ""

    return f"不认识的子命令 {telegram.esc(cmd)}\n\n{LLM_HELP}", ""


HELP = (
    "把东西发给我就行：\n"
    "· 纯文字 → 存成一条想法\n"
    "· 链接 → 抓正文、整理、存成资料\n"
    "· 消息含链接时忽略附带文字；想法请单独发一条\n"
    "· 图片 → OCR 后存成资料\n"
    "· PDF → 中文总结 + PDF 原件\n"
    "· 明确回复引用时才关联：文字回复链接，或链接回复文字\n"
    "· 在 Notion 资料页点「记下想法」→ 下一条消息记成该资料的笔记；点「取消」退出\n"
    "· 改已发出的文字 → 笔记会更新成最新版本\n"
    "\n命令：\n"
    "· /staged 查看暂存内容、等待 LLM 的消息和处理失败原因\n"
    "· /retry 重试暂存区里没抓到正文的链接，并重跑「需处理」的材料\n"
    "· /llm  查看或切换 LLM 服务商和模型\n"
    "· /tags 管理标签词表；回复已入库消息可修改该页面标签"
)


def is_command(text: str) -> bool:
    """Telegram commands are a control channel, not content. /start in
    particular is sent automatically the first time a chat opens."""
    return text.strip().startswith("/")


# Telegram sends a dozen kinds of message this bot has no path for. They used
# to fall out of dispatch as ("", ""), which main reads as "an interactive
# command answered on its own" and deletes the 「收到」 ack -- so a voice note
# got a receipt that then vanished, with nothing stored and nothing said. From
# the chat that is indistinguishable from the message being swallowed. Every
# one of these now says plainly that it was not stored, and points at the way
# round where there is one.
_UNSUPPORTED = (
    ("voice", "🎤 暂不支持语音消息，这条没有存下来。\n"
              "先用系统听写转成文字发过来，那条就会存成想法。"),
    ("video_note", "🎬 暂不支持视频消息，这条没有存下来。"),
    ("video", "🎬 暂不支持视频文件，这条没有存下来。\n"
              "发链接可以存下标题和出处。"),
    ("animation", "🎬 暂不支持动图，这条没有存下来。"),
    ("audio", "🎵 暂不支持音频文件，这条没有存下来。"),
    ("sticker", "🙂 贴纸就不存了。"),
    ("location", "📍 暂不支持位置，这条没有存下来。"),
    ("contact", "👤 暂不支持联系人，这条没有存下来。"),
    ("poll", "📊 暂不支持投票，这条没有存下来。"),
)


def unsupported_note(msg: dict) -> str:
    """What to tell the user about a message we have no path for."""
    for key, note in _UNSUPPORTED:
        if msg.get(key):
            return note
    return "🤔 这种消息我还不知道怎么处理，没有存下来。"


def first_url(msg: dict) -> str:
    """The link this message is about, or "". Used to key the parked-URL index
    from main, which only ever sees the raw update."""
    text = msg.get("text") or msg.get("caption") or ""
    urls = extract.find_urls(text)
    return urls[0] if urls else ""


def dispatch(msg: dict, is_edit: bool, say=None,
             update_id: int | None = None) -> tuple[str, str]:
    """`say` reports progress at stage boundaries. Long ingestion is otherwise
    indistinguishable from a hang, so every step that can take seconds tells
    the user what it is doing rather than going quiet."""
    say = say or (lambda _t: None)
    text = msg.get("text") or msg.get("caption") or ""

    from . import tag_ui
    if tag_ui.handle_reply(msg, is_edit):
        return "", ""
    if not is_edit and is_command(text):
        parts = text.strip().split()
        cmd = parts[0].lower().split("@")[0]
        if cmd == "/tags":
            tag_ui.open_menu(msg)
            return "", ""
        if cmd == "/staged":
            from . import staging_ui
            return telegram.esc(staging_ui.report(msg['chat']['id'], parts[1:])), ""
        if cmd == "/retry":
            return _handle_retry(say)
        if cmd == "/llm":
            return _handle_llm(parts[1:], say, msg["chat"]["id"], msg["message_id"])
        if cmd == "/start" and len(parts) > 1:
            return _handle_note_start(msg, parts[1])
        return HELP, ""
    if is_edit:
        return _handle_edit(msg, text)
    if (msg.get('photo') or msg.get('document')) and extract.find_urls(text):
        msg = {**msg, 'text': '', 'caption': ''}
    if msg.get("photo"):
        group_id = msg.get("media_group_id")
        if group_id:
            members = store.media_group_members(msg["chat"]["id"], group_id)
            # A straggler that arrived after the page was built is not in the
            # member list, and must not rebuild the whole album a second time.
            mine = any(m.get("message_id") == msg["message_id"] for m in members)
            if len(members) > 1 and mine:
                return _handle_photo_group(msg, members, say)
        return _handle_photo(msg, say)
    if msg.get("document"):
        return _handle_document(msg, say)

    urls = extract.find_urls(text)
    if urls:
        return _handle_url(msg, urls[0], '', say, update_id)
    if text.strip():
        return _handle_thought(msg, text)
    return unsupported_note(msg), ""
