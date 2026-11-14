"""Turn a raw artifact (URL / PDF / image) into text we can put in Notion.

Nothing here talks to an LLM -- this is the mechanical layer.
"""
import base64
import io
import logging
import re

import httpx

from . import config
from .thumbnails import html_thumbnail, html_candidates

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; kb-bot/1.0)"
URL_RE = re.compile(r"https?://[^\s<>\"'）】」）]+")
_MARKDOWN_LINK = re.compile(r"\[((?:\\.|[^\]\\])*)\]\(\s*(<?)(https?://)")
_LINK_END = re.compile(r'''\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\([^()]*\))?\s*\)''')
_URL_PUNCT = ".,;:!?、。，"


def _url_end(text: str, start: int) -> int:
    """Stop at a wrapper's closing bracket, retaining balanced URL brackets."""
    match = URL_RE.match(text, start)
    end = match.end() if match else start
    stack = []
    i = start
    while i < end:
        char = text[i]
        if char == "\\" and i + 1 < end and text[i + 1] in "()[]":
            i += 2
            continue
        if char in "([":
            stack.append(char)
        elif char in ")]":
            if not stack or stack[-1] != {')': '(', ']': '['}[char]:
                return i
            stack.pop()
        i += 1
    return end


def _url_spans(text: str) -> list[tuple[str, int, int, str]]:
    """Return destination, source span and retained label in source order.

    A Markdown link is one occurrence: its label can contain another URL, but
    only its destination is fetched. Spans also let note extraction remove the
    whole wrapper without deleting unrelated copies of a URL from the prose.
    """
    links = []
    for match in _MARKDOWN_LINK.finditer(text):
        start = match.start(3)
        end = _url_end(text, start)
        closing = end
        if match.group(2):
            if text[end:end + 1] != '>':
                continue
            closing += 1
        tail = _LINK_END.match(text, closing)
        if not tail:
            continue
        url = re.sub(r"\\([()\[\]])", r"\1", text[start:end])
        label = match.group(1)
        # A URL used as the label is presentation, not an attached thought.
        replacement = '' if label.strip() == url else label
        links.append((url, match.start(), tail.end(), replacement))

    spans = list(links)
    for match in URL_RE.finditer(text):
        if any(start <= match.start() < end for _, start, end, _ in links):
            continue
        end = _url_end(text, match.start())
        url = text[match.start():end].rstrip(_URL_PUNCT)
        if url:
            spans.append((url, match.start(), match.start() + len(url), ''))
    return sorted(spans, key=lambda item: item[1])


def find_urls(text: str) -> list[str]:
    return [url for url, *_ in _url_spans(text or "")]


def without_first_url(text: str, *, keep_label: bool = True) -> str:
    spans = _url_spans(text or "")
    if not spans:
        return text or ""
    _, start, end, label = spans[0]
    return text[:start] + (label if keep_label else '') + text[end:]


# 2310.06825, 2310.06825v2, math/0211159 -- the version suffix has to come off
# before the API sees it: export.arxiv.org answers an id_list with a version
# with no entry at all rather than an error.
_ARXIV_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(?P<id>[\w.\-/]+?)(?:v\d+)?(?:\.pdf)?(?:[?#].*)?$"
    r"|arxiv\.org/(?P<bare>\d{4}\.\d{4,5})(?:v\d+)?$"
    r"|10\.48550/arxiv\.(?P<doi>[\w.\-/]+?)(?:v\d+)?$",
    re.I)
ARXIV_NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def arxiv_id(url: str) -> str:
    """The bare arXiv id in this URL, or "". Version suffixes are stripped."""
    m = _ARXIV_RE.search((url or "").strip())
    if not m:
        return ""
    return (m.group("id") or m.group("bare") or m.group("doi") or "").strip()


# Any DOI sitting in the URL path. doi.org and dl.acm.org put it there
# verbatim; publishers that do not (nature.com, openreview) fall through to the
# ordinary web path, which is the honest outcome -- guessing a DOI from a
# publisher's own URL scheme breaks the moment they change it.
_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s?#&]+)", re.I)


def doi_from_url(url: str) -> str:
    """The DOI in this URL, or "". arXiv's own DOI is excluded: it has a better
    source of its own."""
    m = _DOI_RE.search((url or "").strip())
    if not m:
        return ""
    doi = m.group(1).rstrip(").,;")
    return "" if doi.lower().startswith("10.48550/") else doi


def _strip_jats(xml: str) -> str:
    """Crossref returns abstracts as JATS markup, tags and all."""
    txt = re.sub(r"<jats:title>.*?</jats:title>", " ", xml or "", flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def fetch_crossref(doi: str) -> dict:
    """Paper metadata for any DOI, from Crossref.

    The counterpart to fetch_arxiv for everything that is not on arXiv. Note
    `event.name` -- for a conference paper that is the actual conference, which
    neither arXiv nor a publisher's HTML reliably gives. Abstracts are often
    missing (ACM does not deposit them), so the caller falls back elsewhere.
    """
    blank = {"ok": False, "error": "", "title": "", "text": "", "authors": [],
             "published": "", "doi": doi, "journal_ref": "", "citations": None}
    try:
        r = httpx.get(f"https://api.crossref.org/works/{doi}",
                      timeout=config.FETCH_TIMEOUT,
                      headers={"User-Agent": f"{UA} (mailto:{config.CROSSREF_MAILTO})"})
        if r.status_code == 404:
            return {**blank, "error": f"Crossref 上没有 {doi}"}
        r.raise_for_status()
        m = r.json()["message"]
    except Exception as e:
        return {**blank, "error": f"Crossref 失败: {e}"}

    parts = ((m.get("issued") or {}).get("date-parts") or [[]])[0]
    published = "-".join(f"{p:02d}" if i else str(p)
                         for i, p in enumerate(parts[:3])) if parts else ""
    abstract = _strip_jats(m.get("abstract") or "")
    return {
        "ok": True,          # the bibliography is worth filing even with no abstract
        "error": "",
        "title": (m.get("title") or [""])[0],
        "text": abstract,
        "authors": [" ".join(x for x in (a.get("given"), a.get("family")) if x).strip()
                    for a in (m.get("author") or [])],
        "published": published,
        "doi": m.get("DOI") or doi,
        "journal_ref": ((m.get("event") or {}).get("name")
                        or (m.get("container-title") or [""])[0]),
        "citations": m.get("is-referenced-by-count"),
        "pdf_urls": [link["URL"] for link in m.get("link", [])
                     if link.get("content-type") == "application/pdf" and link.get("URL")],
    }


def fetch_arxiv(paper_id: str) -> dict:
    """Paper metadata from arXiv's own API.

    Scraping the abstract page instead was the original mistake: trafilatura
    cannot tell the abstract from arXivLabs, "Bibliographic and Citation Tools"
    and the rest of the furniture, so all of it ended up filed as 正文. The API
    hands over exactly the fields worth keeping.

    Returns the same {ok, error, title, text} shape as fetch_article, plus the
    metadata fields, so the caller can treat both the same way.
    """
    import xml.etree.ElementTree as ET

    blank = {"ok": False, "error": "", "title": "", "text": "", "authors": [],
             "published": "", "doi": "", "journal_ref": ""}
    try:
        r = httpx.get("https://export.arxiv.org/api/query",
                      params={"id_list": paper_id, "max_results": 1},
                      timeout=config.FETCH_TIMEOUT,
                      headers={"User-Agent": UA})
        r.raise_for_status()
        entry = ET.fromstring(r.text).find("a:entry", ARXIV_NS)
    except Exception as e:
        return {**blank, "error": f"arXiv API 失败: {e}"}
    if entry is None:
        return {**blank, "error": f"arXiv 上没有 {paper_id}"}

    def text_of(tag: str) -> str:
        el = entry.find(tag, ARXIV_NS)
        return re.sub(r"\s+", " ", (el.text or "")).strip() if el is not None else ""

    abstract = text_of("a:summary")
    return {
        "ok": bool(abstract),
        "error": "" if abstract else "arXiv 返回了条目但没有摘要",
        "title": text_of("a:title"),
        "text": abstract,
        "authors": [re.sub(r"\s+", " ", (n.text or "")).strip()
                    for n in entry.findall("a:author/a:name", ARXIV_NS)],
        "published": text_of("a:published")[:10],
        "doi": text_of("arxiv:doi"),
        "journal_ref": text_of("arxiv:journal_ref"),
        "pdf_urls": ["https://arxiv.org/pdf/" + paper_id],
    }


def semantic_scholar(ident: str) -> dict:
    """Venue, citation count and abstract for `arXiv:<id>` or `DOI:<doi>`.

    Best effort on purpose. Without an API key the anonymous pool is shared and
    tight -- measured: one call succeeds and the next one seconds later gets a
    429 -- so an empty answer is the ordinary case, not a fault. Setting
    S2_API_KEY (free, semanticscholar.org/product/api) removes that.
    """
    headers = {"User-Agent": UA}
    if config.S2_API_KEY:
        headers["x-api-key"] = config.S2_API_KEY
    try:
        r = httpx.get(
            f"https://api.semanticscholar.org/graph/v1/paper/{ident}",
            params={"fields": "venue,citationCount,year,abstract"},
            timeout=10.0, headers=headers)
        if r.status_code != 200:
            log.info("semantic scholar %s for %s", r.status_code, ident)
            return {}
        d = r.json()
    except Exception as e:
        log.info("semantic scholar unavailable: %s", e)
        return {}
    venue = (d.get("venue") or "").strip()
    # A preprint's venue comes back as "arXiv.org", which says nothing the
    # 来源平台 property does not already say. Leave the field empty instead.
    if venue.lower() in ("arxiv.org", "arxiv"):
        venue = ""
    return {"venue": venue, "citations": d.get("citationCount"),
            "abstract": (d.get("abstract") or "").strip()}


def _inner_html(html: str, marker: str) -> str:
    """The contents of the element whose opening tag contains `marker`.

    Walks the div nesting rather than matching a closing tag with a regex --
    an article body is full of nested divs and the first </div> is never the
    right one.
    """
    start = html.find(marker)
    if start < 0:
        return ""
    open_end = html.find(">", start)
    if open_end < 0:
        return ""
    depth, i = 1, open_end + 1
    while depth and i < len(html):
        nxt_open = html.find("<div", i)
        nxt_close = html.find("</div", i)
        if nxt_close < 0:
            break
        if 0 <= nxt_open < nxt_close:
            depth += 1
            i = nxt_open + 4
        else:
            depth -= 1
            i = nxt_close + 5
    return html[open_end + 1:max(open_end + 1, i - 5)]


def _tags_to_text(fragment: str) -> str:
    import html as html_mod
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", fragment, flags=re.S | re.I)
    txt = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = html_mod.unescape(txt).replace("\xa0", " ")
    lines = [ln.strip() for ln in txt.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def fetch_wechat(url: str) -> dict:
    """A 公众号 article, read from its own markup rather than by heuristics.

    WeChat leaves <title> empty and fills it with JS, and hides the body behind
    `visibility: hidden` until JS reveals it -- so a generic extractor gets a
    partial answer at best. The real title sits in a `msg_title` script var and
    the body in #js_content, both stable, and reading them directly yields more
    text than trafilatura finds.
    """
    try:
        r = httpx.get(url, timeout=config.FETCH_TIMEOUT,
                      follow_redirects=True, headers={"User-Agent": UA})
        r.raise_for_status()
    except Exception as e:
        return {"ok": False, "error": f"抓取失败: {e}", "title": "", "text": ""}

    html = r.text
    m = re.search(r"var\s+msg_title\s*=\s*['\"](.*?)['\"]", html, re.S)
    title = _tags_to_text(m.group(1)) if m else ""
    if not title:
        title = _tags_to_text(_inner_html(html, 'id="activity-name"'))
    text = _tags_to_text(_inner_html(html, 'id="js_content"'))

    if not text:
        # Either the article is gone, or WeChat decided this caller looks like
        # a robot and served an interstitial instead. Say which, because the
        # two need completely different responses.
        looks_blocked = any(k in html for k in ("环境异常", "去验证", "操作频繁"))
        return {"ok": False, "title": title, "text": "",
                "error": ("微信要求验证，服务器 IP 被判定为异常访问"
                          if looks_blocked else
                          f"页面里没有 js_content（收到 {len(html)} 字节，"
                          "可能是删除/迁移，或需要在微信内打开）")}
    return {"ok": True, "error": "", "title": title, "text": text,
            "thumbnail": html_thumbnail(html, str(getattr(r, "url", url))),
            "art_candidates": html_candidates(html, str(getattr(r, "url", url)), text,
                                               _inner_html(html, 'id="js_content"'))}


# These whole-paragraph templates come from real HTTP-200 false successes:
# EAA's member-only resources and Alpha Eta Rho's login/cookie screen. A gate
# must account for the ENTIRE extraction; mentioning login in an article is
# not a failure, and there is deliberately no minimum article length.
_MEMBER_GATE = re.compile(
    r"This page is a premium content area exclusively for .+? members!"
    r"|You must be a member to view this page\. Login below to access this content\.", re.I)
_GATE_INSTRUCTIONS = re.compile(
    r"If you do not currently have an .+? account, register by clicking the button below\. "
    r"Once you are logged in as a member, you will be able to view this page\."
    r"|Cookies are not enabled and they must be in order for you to remain logged in for your entire session\. "
    r"Learn how to enable cookies here\."
    r"|If you think this is an error, please email us at [^\s]+\.", re.I)


def _only_access_gate(title: str, text: str) -> bool:
    lines = [re.sub(r"\s+", " ", p).strip() for p in text.splitlines() if p.strip()]
    if not any(_MEMBER_GATE.fullmatch(p) for p in lines):
        return False
    heading = title.split("|", 1)[0].strip().casefold()
    return all(_MEMBER_GATE.fullmatch(p) or _GATE_INSTRUCTIONS.fullmatch(p)
               or p.casefold() == heading for p in lines)


def dynamic_shell(html: str) -> bool:
    """An executable app root, not a short article or arbitrary empty page."""
    from lxml import html as lhtml
    try:
        doc = lhtml.fromstring(html)
        return bool(doc.xpath('//script[@src or @type="module"]') and doc.xpath(
            '//*[@id="root" or @id="app" or @id="__next" or @id="__nuxt" or @ng-app]'))
    except (ValueError, lhtml.etree.ParserError):
        return False


def parse_article_html(html: str, url: str) -> dict:
    """Static and rendered pages share content, gate and project checks."""
    from lxml import html as lhtml
    try:
        import trafilatura
    except ImportError:  # pragma: no cover
        trafilatura = None
    from .web_content import content_pending
    title, text = "", ""
    pending = False
    headings = []
    if trafilatura is not None:
        meta = trafilatura.extract_metadata(html)
        if meta and meta.title:
            title = meta.title
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    from . import paper_project
    title = paper_project.page_title(html) or title
    try:
        doc = lhtml.fromstring(html)
        body_text = re.sub(r'\s+', ' ', ' '.join(doc.xpath('//body//text()[not(ancestor::script) and not(ancestor::style)]'))).strip()
        # Explicit challenges must not be "solved" by rendering. Avoid matching
        # an article merely discussing CAPTCHA or login techniques.
        challenge = title.casefold().strip() in {'just a moment...', 'attention required! | cloudflare', 'verify you are human'}
        login_only = (title.casefold().strip() in {'login', 'log in', 'sign in', '登录', '登入'}
                      and bool(doc.xpath('//input[@type="password"]'))
                      and not doc.xpath('//article//p|//main//p'))
        if challenge or login_only or _only_access_gate(title, body_text):
            return {"ok": False, "error": "页面只返回登录 / 验证 / cookie 提示，没有取得正文",
                    "failure_kind": "access_gate", "title": title, "text": ""}
        for node in doc.xpath('//nav|//footer|//aside|//*[@aria-hidden="true"]'):
            if node.getparent() is not None:
                node.drop_tree()
        # Real leaderboard headers often live in sort buttons. Trafilatura
        # drops buttons as controls, losing the meaning of every numeric cell.
        for node in doc.xpath('//table//button'):
            node.drop_tag()
        slides = set()
        for node in doc.xpath('//*[contains(concat(" ", normalize-space(@class), " "), " swiper-slide ") or @aria-roledescription="slide"]'):
            signature = re.sub(r'\s+', ' ', node.text_content()).strip()
            if signature and signature in slides and node.getparent() is not None:
                node.drop_tree()
            slides.add(signature)
        # Use complete headings, including inline spans, rather than fragments.
        headings = [title, *[h.text_content() for h in doc.xpath('//h1|//h2|//h3|//h4|//h5|//h6')]]
        roots = doc.xpath('//article|//main|//*[@role="main"]|//body')
        scope = next((n for n in roots if n.tag != 'body'), roots[0] if roots else doc)
        import copy
        visible = copy.deepcopy(scope)
        for node in visible.xpath('.//script|.//style|.//noscript|.//header'):
            node.drop_tree()
        pending = content_pending(visible.text_content(), headings)
        cleaned = lhtml.tostring(doc, encoding='unicode')
    except (ValueError, lhtml.etree.ParserError):
        cleaned = html
    if trafilatura is not None:
        text = trafilatura.extract(cleaned, include_comments=False,
                                   include_tables=True, favor_precision=True) or ""
    if not text or pending or content_pending(text, headings):
        kind = "dynamic_shell" if dynamic_shell(html) else "empty_body"
        return {"ok": False, "error": "页面抓到了，但没有提取出正文",
                "failure_kind": kind, "title": title, "text": ""}
    if _only_access_gate(title, text):
        return {"ok": False, "error": "页面只返回会员登录 / cookie 提示，没有取得正文",
                "failure_kind": "access_gate", "title": title, "text": ""}
    project = paper_project.detect(html, url, title)
    return {"ok": True, "error": "", "failure_kind": "", "title": title, "text": text,
            **({"paper_project": project} if project else {}),
            "thumbnail": html_thumbnail(html, url),
            "art_candidates": html_candidates(html, url, text)}


def fetch_article(url: str) -> dict:
    """Fetch a page and pull out readable article text.

    Returns {title, text, ok, error}. A failure is reported, never faked: the
    caller keeps the link in staging without creating an incomplete material.
    """
    try:
        r = httpx.get(url, timeout=config.FETCH_TIMEOUT,
                      follow_redirects=True, headers={"User-Agent": UA})
        r.raise_for_status()
    except Exception as e:
        return {"ok": False, "error": f"抓取失败: {e}", "title": "", "text": "",
                "failure_kind": "http_error" if isinstance(e, httpx.HTTPStatusError) else "network_error"}
    return parse_article_html(r.text, str(getattr(r, "url", url)))


def pdf_text(data: bytes) -> dict:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages]
        text = "\n\n".join(pages).strip()
        meta = reader.metadata or {}
        title = (meta.get("/Title") or "").strip()
        return {"ok": bool(text), "title": title, "text": text,
                "pages": len(reader.pages),
                "error": "" if text else "PDF 没有文本层，可能是扫描件，需要 OCR"}
    except Exception as e:
        return {"ok": False, "title": "", "text": "", "pages": 0,
                "error": f"PDF 解析失败: {e}"}


# A scanned page costs image tokens, and a 200-page scan would cost absurdly
# many. Ten pages is enough to tell what a document is and to carry a short
# paper; beyond that the page says how much was read.
MAX_SCAN_PAGES = 10


def pdf_page_images(data: bytes, limit: int = MAX_SCAN_PAGES) -> tuple[list, int]:
    """Render the first pages of a PDF to JPEGs, for a PDF with no text layer.

    Returns (images, total_pages). Uses pypdfium2 rather than PyMuPDF: same pure
    wheel, smaller, and BSD instead of AGPL.
    """
    try:
        import pypdfium2
    except ImportError:  # pragma: no cover
        log.warning("pypdfium2 missing, cannot render a scanned PDF")
        return [], 0
    try:
        doc = pypdfium2.PdfDocument(data)
        total = len(doc)
        out = []
        for i in range(min(total, limit)):
            pil = doc[i].render(scale=2).to_pil()
            buf = io.BytesIO()
            pil.convert("RGB").save(buf, format="JPEG", quality=80)
            out.append((buf.getvalue(), f"page-{i + 1}.jpg"))
        return out, total
    except Exception as e:
        log.warning("could not render the PDF: %s", e)
        return [], 0


def ocr_image(data: bytes) -> dict:
    """Google Cloud Vision DOCUMENT_TEXT_DETECTION.

    Free tier is 1000 units/month, which is well above personal use. Called via
    REST so we don't pull in another SDK.
    """
    try:
        import google.auth
        import google.auth.transport.requests

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        body = {
            "requests": [{
                "image": {"content": base64.b64encode(data).decode()},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                "imageContext": {"languageHints": ["zh", "en"]},
            }]
        }
        r = httpx.post(
            "https://vision.googleapis.com/v1/images:annotate",
            json=body, timeout=60.0,
            headers={"Authorization": f"Bearer {creds.token}"},
        )
        r.raise_for_status()
        resp = r.json()["responses"][0]
        if "error" in resp:
            return {"ok": False, "text": "", "error": str(resp["error"])}
        text = (resp.get("fullTextAnnotation") or {}).get("text", "").strip()
        return {"ok": bool(text), "text": text,
                "error": "" if text else "图片里没有识别到文字"}
    except Exception as e:
        return {"ok": False, "text": "", "error": f"OCR 失败: {e}"}


# Platforms whose article pages a plain HTTP GET cannot read at all. Zhihu
# answers 403 without a `d_c0` device cookie, which is computed by its own
# JavaScript and cannot be obtained by fetching. These skip trafilatura and go
# straight to the fallback parser.
_NO_PLAIN_HTTP = ("zhihu", "xiaohongshu", "xhslink")


def parsehub_platform(url: str) -> str | None:
    """The name of the parsehub parser for this URL, or None if there is none.

    Import is deferred: parsehub pulls in yt-dlp and friends, and the common
    case (an ordinary web page) never needs it.
    """
    try:
        from parsehub import ParseHub

        # get_parser returns the parser *class*, or None when nothing matches.
        parser = ParseHub().get_parser(url)
        return getattr(parser, "__name__", None) if parser is not None else None
    except Exception:
        return None


def repair_xhs_image_urls(images: list[str], thumbnail: str) -> list[str]:
    """Restore the Rednote CDN only when the source thumbnail proves the rewrite."""
    cdn = re.match(r'https://sns-i\d+\.rednotecdn\.com/notes_pre_post/', thumbnail)
    if not cdn or not images:
        return images
    repaired = []
    for url in images:
        image = re.fullmatch(r'https?://sns-img-hw\.xhscdn\.com/([a-z0-9]+(?:\?[^#\s]*)?)', url)
        if not image:
            return images
        repaired.append(cdn[0] + image[1])
    return repaired if repaired[0] == thumbnail else images


def parse_via_parsehub(url: str, cookie: dict | None = None) -> dict:
    """Fallback extractor for platforms that defeat a plain fetch.

    parsehub reaches these sites two ways: reading the state blob a page embeds
    (小红书), or reimplementing the site's request signature (知乎's x-zse-96).
    Both break when the site changes, so a failure here is expected and is
    reported, never papered over -- the caller falls back to 等待采集.
    """
    try:
        from parsehub import ParseHub
        result = ParseHub().parse_sync(url, cookie=cookie) if cookie \
            else ParseHub().parse_sync(url)
    except Exception as e:
        return {"ok": False, "title": "", "text": "", "platform": "",
                "images": [], "error": f"解析失败: {type(e).__name__}: {e}"}

    title = (getattr(result, "title", "") or "").strip()
    text = (getattr(result, "content", "") or "").strip()
    platform = getattr(getattr(result, "platform", None), "id", "") or ""
    media = getattr(result, "media", None)
    media = list(media) if isinstance(media, (list, tuple)) else ([media] if media else [])
    thumbnail = next((getattr(m, "thumb_url", "") for m in media
                      if getattr(m, "thumb_url", "")), "")
    from parsehub.types.media_ref import VideoRef
    post_type = getattr(result, "type", "")
    post_type = getattr(post_type, "value", post_type)
    videos = [m.url for m in media if isinstance(m, VideoRef) and m.url]
    # LivePhotoRef.url is its still image; video_url is deliberately excluded.
    images = [m.url for m in media if not isinstance(m, VideoRef) and getattr(m, "url", "")]
    images = repair_xhs_image_urls(images, thumbnail)
    media_type = "video" if videos or post_type == "video" else str(post_type)
    if not (title or text or images or videos):
        return {"ok": False, "title": "", "text": "", "platform": platform,
                "images": [], "error": "解析器返回了空内容"}
    return {"ok": True, "title": title, "text": text, "platform": platform,
            "images": images, "videos": videos, "media_type": media_type,
            "thumbnail": thumbnail, "error": "",
            "resolved_url": getattr(result, 'raw_url', '') or url,
            "art_candidates": {"shares": [thumbnail] if thumbnail else [], "body": images}}


# Formats a vision endpoint can be expected to decode. Anything else is left
# alone rather than mislabelled -- one undecodable image fails the whole batch.
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_image_type(blob: bytes) -> str | None:
    """The real format, from the bytes.

    Neither source can be trusted: 小红书's CDN serves HEIC under
    `Content-Type: image/jpeg`, and parsehub reports `ext=jpeg` for it too.
    """
    for magic, mime in _MAGIC:
        if blob.startswith(magic):
            return mime
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return None


MAX_IMAGES = 9
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024


class DownloadedImages(list):
    """Vision copies, with unchanged source bytes kept separately for archival."""

    def __init__(self):
        super().__init__()
        self.originals: list[tuple[int, bytes, str]] = []


def original_image_type(blob: bytes) -> str:
    mime = sniff_image_type(blob)
    if mime:
        return mime
    if blob[4:8] == b"ftyp":
        brands = [blob[8:12], *(blob[i:i + 4] for i in range(16, min(len(blob), 64), 4))]
        if any(b in {b"heic", b"heix", b"hevc", b"hevx"} for b in brands):
            return "image/heic"
        if any(b in {b"mif1", b"msf1"} for b in brands):
            return "image/heif"
    return "application/octet-stream"


def vision_image(blob: bytes) -> tuple[bytes, str]:
    """Convert unsupported raster formats without using the thumbnail sizing.

    Keep text at source resolution where possible. Pixel and encoded-byte caps
    bound decoding and model payloads; source bytes are never modified.
    """
    mime = sniff_image_type(blob)
    if mime:
        return blob, mime
    from PIL import Image, ImageOps
    import pillow_heif

    pillow_heif.register_heif_opener()
    with Image.open(io.BytesIO(blob)) as opened:
        if opened.width * opened.height > 24_000_000:
            raise ValueError("too many pixels")
        image = ImageOps.exif_transpose(opened).convert("RGB")
    image.thumbnail((4096, 4096))
    buf = io.BytesIO()
    image.save(buf, "PNG", optimize=True)
    if buf.tell() <= MAX_IMAGE_BYTES:
        return buf.getvalue(), "image/png"
    for _ in range(8):
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=95, optimize=True)
        if buf.tell() <= MAX_IMAGE_BYTES:
            return buf.getvalue(), "image/jpeg"
        image.thumbnail((max(1, int(image.width * .8)), max(1, int(image.height * .8))))
    raise ValueError("converted image too large")


def download_images(urls: list[str]) -> tuple[list[tuple[bytes, str]], list[str]]:
    """Fetch post images, bounded in count and total size.

    Returns (vision copies with originals attached, every unprocessed URL).

    A picture-heavy post would otherwise blow up both the request payload and
    the token bill, so the caps are hard and failures are skipped rather than
    raised -- missing one image is not worth losing the material, and one
    undecodable image must not fail the batch that carries the other six.
    """
    out = DownloadedImages()
    skipped: list[str] = list(urls[MAX_IMAGES:])
    total = vision_total = 0
    for index, url in enumerate(urls[:MAX_IMAGES]):
        try:
            r = httpx.get(url, timeout=config.FETCH_TIMEOUT,
                          follow_redirects=True, headers={"User-Agent": UA})
            r.raise_for_status()
        except Exception as e:
            log.warning("image download failed %s: %s", url[:80], e)
            skipped.append(url)
            continue
        blob = r.content
        if len(blob) > MAX_IMAGE_BYTES:
            log.info("skipping oversized image (%s bytes)", len(blob))
            skipped.append(url)
            continue
        if total + len(blob) > MAX_TOTAL_IMAGE_BYTES:
            skipped.append(url)
            continue
        out.originals.append((index, blob, original_image_type(blob)))
        total += len(blob)
        try:
            copy, mime = vision_image(blob)
        except Exception as e:
            log.info("image conversion failed (%r): %s", blob[:12], type(e).__name__)
            skipped.append(url)
            continue
        if vision_total + len(copy) > MAX_TOTAL_IMAGE_BYTES:
            skipped.append(url)
            continue
        out.append((copy, mime))
        vision_total += len(copy)
    return out, skipped


def parse_cookie_for(url: str) -> dict | None:
    """Zhihu needs only `d_c0`, a device id it hands to anonymous visitors too.

    Its own error text says "已登录的 Cookie", but the code reads d_c0 and never
    z_c0 (the login token), so no account is involved and none can be banned.
    Take it from a private window so no login cookie rides along.
    """
    from . import config

    if "zhihu" in url.lower() and config.ZHIHU_DC0:
        return {"d_c0": config.ZHIHU_DC0}
    return None


def guess_platform(url: str) -> str:
    """Map a host to one of the 来源平台 options that already exist in Notion."""
    u = url.lower()
    if "xiaohongshu" in u or "xhslink" in u:
        return "小红书"
    if "zhihu" in u:
        return "知乎"
    if "bilibili" in u or "b23.tv" in u:
        return "B站"
    if "youtube" in u or "youtu.be" in u:
        return "YouTube"
    if "mp.weixin.qq.com" in u:
        return "微信"
    if "arxiv.org" in u or "doi.org" in u:
        return "论文"
    return "网页"


def plain_http_blocked(url: str) -> bool:
    """True when fetching the HTML directly is pointless, so we skip straight
    to the fallback parser instead of burning a request on a guaranteed 403."""
    return any(k in url.lower() for k in _NO_PLAIN_HTTP)
