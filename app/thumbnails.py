"""Bounded decorative images: source candidates, safe display copies and reuse.

Originals are never changed. Notion upload IDs (not signed download URLs) are
cached; invalid/archived uploads are discarded. Decoration never gates ingestion.
"""

import hashlib
import io
import logging
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from lxml import html as html_parser
from PIL import Image, ImageOps
from .art_categories import fallback_category as choose_fallback_category

log = logging.getLogger(__name__)
ASSETS = Path(__file__).parent / "assets"
MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 24_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
PLATFORMS = {
    "小红书": "xiaohongshu",
    "知乎": "zhihu",
    "微信": "wechat",
    "B站": "bilibili",
    "YouTube": "youtube",
    "ChatGPT": "openai",
    "Gemini": "googlegemini",
}
EMOJI = {
    "图片": "🖼️",
    "PDF": "📄",
    "论文": "📑",
    "网页": "🌐",
    "帖子": "📌",
    "视频": "🎬",
    "随想": "💭",
    "阅读笔记": "📝",
    "研究分析": "🔬",
    "聊天提炼": "💬",
}
_memory = {}
_downloads = {}


def _url(value, base):
    if not value:
        return ""
    value = urljoin(base, value or "")
    return value if urlparse(value).scheme in {"http", "https"} else ""


def youtube_thumbnail(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = parsed.path.strip("/").split("/")
    ident = ""
    if host == "youtu.be":
        ident = parts[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parts[0] == "watch":
            ident = parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) > 1 and parts[0] in {"shorts", "embed", "live"}:
            ident = parts[1]
    return (
        f"https://i.ytimg.com/vi/{ident}/hqdefault.jpg"
        if re.fullmatch(r"[\w-]{11}", ident, re.ASCII)
        else ""
    )


def _logo_url(url):
    return bool(
        re.search(
            r"(?:^|[/_.-])(logo|favicon|apple-touch-icon|site-icon)(?:[/_.-]|$)",
            url.lower(),
        )
    )


def html_candidates(html, page_url, text="", body_html=None):
    """Only collect body images from a located article, never all page images."""
    result = {"shares": [], "body": [], "logos": [], "site": page_url}
    try:
        doc = html_parser.fromstring(html)
        for meta in doc.xpath("//meta[@content]"):
            name = (meta.get("property") or meta.get("name") or "").lower()
            if name in {
                "og:image",
                "og:image:secure_url",
                "twitter:image",
                "twitter:image:src",
            }:
                u = _url(meta.get("content"), page_url)
                if u:
                    result["logos" if _logo_url(u) else "shares"].append(u)
        icons = []
        for link in doc.xpath("//link[@href]"):
            rel = set((link.get("rel") or "").lower().split())
            if not rel.intersection(
                {"icon", "apple-touch-icon", "apple-touch-icon-precomposed"}
            ):
                continue
            u = _url(link.get("href"), page_url)
            sizes = [int(s) for s in re.findall(r"(\d+)x\d+", link.get("sizes") or "")]
            score = max(sizes or [180 if "apple-touch-icon" in rel else 32])
            if u:
                icons.append((score, u))
        result["logos"] = [u for _, u in sorted(icons, reverse=True)] + result["logos"]
        result["logos"].append(_url("/favicon.ico", page_url))
        result["shares"] = [u for u in result["shares"] if u not in result["logos"]]
        if body_html is not None:
            body = html_parser.fromstring(body_html) if body_html.strip() else None
        else:
            bodies = doc.xpath(
                '//article|//main|//*[@role="main"]|//*[@itemprop="articleBody"]|'
                '//div[contains(@class,"post-content") or contains(@class,"entry-content") or '
                'contains(@class,"article-content") or contains(@class,"post-body")]'
            )
            probes = [
                re.sub(r"\s+", " ", line).strip()[:90]
                for line in text.splitlines()
                if len(line.strip()) > 25
            ][:12]
            ranked = []
            for b in bodies:
                plain = re.sub(r"\s+", " ", b.text_content())
                matches = sum(p in plain for p in probes)
                if matches or (not probes and text.strip() and text.strip() in plain):
                    ranked.append((matches, -len(plain), b))
            body = max(ranked, key=lambda r: r[:2])[2] if ranked else None
        if body is not None:
            for image in body.xpath(".//img"):
                ancestors = [image, *image.iterancestors()]
                if any(
                    e.tag in {"nav", "aside", "footer", "header"}
                    or re.search(
                        r"(^|[\s_-])(avatar|advert|advertisement|ads|comments?|recommend|navigation)([\s_-]|$)",
                        (e.get("class") or "") + " " + (e.get("id") or ""),
                        re.I,
                    )
                    for e in ancestors
                ):
                    continue
                if any(
                    (image.get(k) or "").isdigit() and int(image.get(k)) < 100
                    for k in ("width", "height")
                ):
                    continue
                srcset = image.get("data-srcset") or image.get("srcset") or ""
                choices = []
                for part in srcset.split(","):
                    m = re.fullmatch(r"\s*(\S+)\s+(\d+(?:\.\d+)?)([wx])\s*", part)
                    if m:
                        choices.append(
                            (float(m[2]) * (1000 if m[3] == "x" else 1), m[1])
                        )
                sources = [u for _, u in sorted(choices, reverse=True)] + [
                    image.get(k)
                    for k in ("data-src", "data-original", "data-lazy-src", "src")
                ]
                for src in sources:
                    u = _url(src, page_url)
                    if u and not _logo_url(u):
                        result["body"].append(u)
        for key in ("shares", "body", "logos"):
            result[key] = list(dict.fromkeys(result[key]))[:6]
    except (ValueError, TypeError):
        pass
    return result


def html_thumbnail(html, page_url):
    return next(iter(html_candidates(html, page_url)["shares"]), "")


def download(url, *, limit=MAX_BYTES, timeout=6):
    from . import extract, work_budget

    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("not an image URL")
    cached = _downloads.get(url)
    if cached and time.monotonic() - cached[0] < 300 and len(cached[1]) <= limit:
        return cached[1]
    started = time.monotonic()
    with httpx.stream(
        "GET",
        url,
        follow_redirects=True,
        timeout=work_budget.timeout(timeout, reserve=5),
        headers={"User-Agent": extract.UA},
    ) as r:
        r.raise_for_status()
        if int(r.headers.get("Content-Length") or 0) > limit:
            raise ValueError("too large")
        data = bytearray()
        for chunk in r.iter_bytes():
            work_budget.timeout(timeout, reserve=5)
            data.extend(chunk)
            if len(data) > limit or time.monotonic() - started > 12:
                raise ValueError("image limit")
    result = bytes(data)
    if len(_downloads) >= 12:
        _downloads.pop(next(iter(_downloads)))
    _downloads[url] = (time.monotonic(), result)
    return result


def normalize(data, mode="cover"):
    """Decode actual bytes, bound pixels, fix EXIF; SVG cannot fetch resources."""
    if not data or len(data) > MAX_BYTES:
        raise ValueError("invalid image size")
    svg = b"<svg" in data[:2048]
    if svg:
        from defusedxml import ElementTree
        import cairosvg

        root = ElementTree.fromstring(data)
        for el in root.iter():
            if el.tag.rsplit("}", 1)[-1] in {
                "script",
                "foreignObject",
                "image",
                "use",
                "style",
            }:
                raise ValueError("external/active SVG")
            for name, value in el.attrib.items():
                if name.rsplit("}", 1)[-1] == "href" or re.search(
                    r"url\s*\(|@import", value, re.I
                ):
                    raise ValueError("linked SVG")
        # Bound the output allocation before Cairo parses source dimensions.
        box = re.split(r"[ ,]+", root.get("viewBox", "").strip())
        if len(box) == 4:
            w, h = float(box[2]), float(box[3])
        else:
            w = float(re.sub(r"[^\d.]", "", root.get("width", "512")) or "512")
            h = float(re.sub(r"[^\d.]", "", root.get("height", "512")) or "512")
        if min(w, h) <= 0 or max(w / h, h / w) > 12:
            raise ValueError("SVG aspect limit")
        scale = min(512 / w, 512 / h)
        data = cairosvg.svg2png(
            bytestring=data,
            output_width=max(1, int(w * scale)),
            output_height=max(1, int(h * scale)),
        )
    else:
        import pillow_heif

        pillow_heif.register_heif_opener()
    with Image.open(io.BytesIO(data)) as opened:
        if opened.width * opened.height > MAX_PIXELS:
            raise ValueError("too many pixels")
        image = ImageOps.exif_transpose(opened).convert("RGBA")
        use_png = (
            svg
            or image.getextrema()[3][0] < 255
            or opened.format == "ICO"
            or mode in {"icon", "logo_cover"}
        )
    original_size = image.size
    if mode in {"icon", "logo_cover"}:
        canvas_size = (384, 384) if mode == "icon" else (600, 400)
        # Tiny favicon stays small instead of becoming a blurry full card.
        edge = min(256 if mode == "icon" else 240, max(image.size) * 4)
        factor = edge / max(image.size)
        image = image.resize(
            (max(1, round(image.width * factor)), max(1, round(image.height * factor)))
        )
        canvas = Image.new("RGBA", canvas_size, "#f7f8fa")
        canvas.alpha_composite(
            image,
            ((canvas.width - image.width) // 2, (canvas.height - image.height) // 2),
        )
        image = canvas
    else:
        image.thumbnail((1400, 1400))
    buf = io.BytesIO()
    if use_png:
        image.save(buf, "PNG", optimize=True)
    else:
        image.convert("RGB").save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue(), "image/png" if use_png else "image/jpeg", original_size


def _cache_get(key):
    from . import config, store, notion, work_budget

    if not config.NOTION_TOKEN:
        return None
    cached = _memory.get(key)
    if cached and time.time() - cached.get("checked", 0) < 300:
        return cached
    try:
        timeout = work_budget.timeout(10, reserve=5)
        if not cached:
            snap = (
                store.db()
                .collection("art_cache")
                .document(hashlib.sha256(key.encode()).hexdigest())
                .get(timeout=timeout, retry=None)
            )
            cached = snap.to_dict() if snap.exists else None
        if not cached or time.time() - cached["at"] > 7 * 86400:
            return None
        obj = notion._call("GET", "/file_uploads/" + cached["id"])
        if obj.get("status") != "uploaded" or obj.get("archived"):
            return None
        cached["checked"] = time.time()
        _memory[key] = cached
        return cached
    except Exception:
        return None


def _remember(key, ident):
    from . import store, work_budget

    value = {"id": ident, "at": time.time(), "checked": time.time()}
    _memory[key] = value
    try:
        timeout = work_budget.timeout(10, reserve=5)
        store.db().collection("art_cache").document(
            hashlib.sha256(key.encode()).hexdigest()
        ).set(value, timeout=timeout, retry=None)
    except Exception:
        log.info("art cache unavailable")


def upload_image(source, mode="cover", *, cache_key="", minimum=0):
    from . import notion, work_budget

    work_budget.timeout(6, reserve=5)
    if cache_key:
        cached = _cache_get(cache_key)
        if cached:
            return {"type": "file_upload", "file_upload": {"id": cached["id"]}}
    data = download(source) if isinstance(source, str) else source
    encoded, mime, size = normalize(data, mode)
    if min(size) < minimum:
        raise ValueError("small content image")
    ident = notion.upload_file(
        encoded, "display.png" if mime == "image/png" else "display.jpg", mime
    )
    if not ident:
        raise ValueError("upload failed")
    if cache_key:
        _remember(cache_key, ident)
    return {"type": "file_upload", "file_upload": {"id": ident}}


def upload_thumbnail(image_url):
    try:
        image = upload_image(image_url)
        return {"name": "缩略图", **image}
    except Exception:
        return None


def note_icon(note_type, provider=""):
    from . import config

    emoji = {"type": "emoji", "emoji": EMOJI.get(note_type, "📝")}
    if not config.NOTION_TOKEN:
        return emoji
    name = PLATFORMS.get(provider) or {
        "随想": "thought",
        "阅读笔记": "note",
        "研究分析": "research",
    }.get(note_type)
    if name:
        try:
            return upload_image(
                (ASSETS / (name + ".svg")).read_bytes(),
                "icon",
                cache_key="asset:" + name + ":icon:v3",
            )
        except Exception:
            pass
    return emoji


def site_generic_images(site):
    """Compare with cached site-home metadata, not guessed filename rules.

    This catches a real case: every sive.rs article advertises the same author
    portrait as og:image. It is not an article illustration. Cache per exact
    origin for a day; failure keeps the supplied candidates and never gates text.
    """
    from . import store, work_budget
    from urllib.parse import urlunsplit

    parsed = urlparse(site)
    if parsed.path in ("", "/") or not parsed.netloc:
        return []
    home = urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
    key = "site-meta:" + home
    cached = _memory.get(key)
    ref = None
    try:
        timeout = work_budget.timeout(4, reserve=5)
        if not cached:
            ref = (
                store.db()
                .collection("art_cache")
                .document(hashlib.sha256(key.encode()).hexdigest())
            )
            snap = ref.get(timeout=timeout, retry=None)
            cached = snap.to_dict() if snap.exists else None
        if cached and time.time() - cached.get("at", 0) < 86400:
            return cached.get("generic", [])
    except Exception:
        pass
    try:
        data = download(home, limit=512 * 1024, timeout=4)
        generic = html_candidates(data.decode("utf-8", "replace"), home)["shares"]
        cached = {"at": time.time(), "generic": generic}
        _memory[key] = cached
        if ref:
            try:
                ref.set(cached, timeout=work_budget.timeout(4, reserve=5), retry=None)
            except Exception:
                pass
        return generic
    except Exception:
        return []


def material_art(
    material_type,
    platform,
    url="",
    candidates=None,
    image_bytes=None,
    pdf_bytes=None,
    fallback_category="",
    source_kind="",
    budget=20,
):
    from . import config, work_budget

    # Preserve time for staged note associations and the retry response.
    seconds = min(budget, work_budget.remaining() - 15)
    if not config.NOTION_TOKEN or seconds <= 5:
        return {}
    with work_budget.limit(seconds):
        return _material_art(material_type, platform, url, candidates, image_bytes,
                             pdf_bytes, fallback_category, source_kind, seconds)


def _material_art(material_type, platform, url, candidates, image_bytes,
                  pdf_bytes, fallback_category, source_kind, budget):
    from . import work_budget
    end = time.monotonic() + budget
    candidates = candidates or {}
    cover = None
    icon = None
    if (
        platform == "网页"
        and candidates.get("shares")
        and image_bytes is None
        and pdf_bytes is None
    ):
        generic = site_generic_images(candidates.get("site") or url)
        candidates = {
            **candidates,
            "shares": [u for u in candidates["shares"] if u not in generic],
        }
    fixed = PLATFORMS.get(platform)
    if material_type == "论文":
        fixed = (
            "arxiv"
            if re.search(r"(^|\.)arxiv\.org$", urlparse(url).hostname or "")
            else "paper"
        )
    if pdf_bytes is not None and not url:
        fixed = "pdf"
    if material_type == "图片":
        fixed = "image"

    def attempt(source, mode="cover", key="", minimum=0):
        if time.monotonic() > end or work_budget.remaining() <= 5:
            return None
        try:
            return upload_image(source, mode, cache_key=key, minimum=minimum)
        except Exception:
            return None

    if fixed:
        icon = attempt(
            (ASSETS / (fixed + ".svg")).read_bytes(),
            "icon",
            "asset:" + fixed + ":icon:v3",
        )
    if image_bytes is not None:
        cover = attempt(image_bytes)
    elif pdf_bytes is not None:
        from .pdf_art import pdf_thumbnail

        image = pdf_thumbnail(pdf_bytes)
        if image:
            cover = attempt(image)
    else:
        for u in [*candidates.get("shares", []), *candidates.get("body", [])]:
            cover = attempt(u, minimum=120)
            if cover:
                break
    if not fixed and platform == "网页":
        site = candidates.get("site") or url
        host = urlparse(site).netloc.lower()  # cache exact host, including subdomains
        for u in candidates.get("logos", []):
            icon = attempt(u, "icon", "site:" + host + ":" + u + ":icon:v2")
            if icon:
                break
    if not cover:
        category = choose_fallback_category(
            material_type,
            fallback_category,
            platform=platform,
            source_kind=source_kind
            or ("document" if pdf_bytes is not None and not url else ""),
        )
        path = ASSETS / ("placeholder-" + category + ".svg")
        cover = attempt(
            path.read_bytes(), key="asset:" + path.stem + ":cover:v3"
        )
    if not icon:
        icon = note_icon("网页" if not fixed else material_type) if work_budget.remaining() > 5 else {"type": "emoji", "emoji": EMOJI.get(material_type, "📎")}
    result = {"icon": icon}
    if cover:
        result["thumbnail"] = {"name": "缩略图", **cover}
    return result


def paper_pdf(url, candidates):
    """Reuse an archived PDF; only explicit arXiv/Crossref PDF associations.

    Fetch/parse limits affect artwork only. No publisher HTML guessing, login,
    project page crawl, or admission-rule changes.
    """
    from . import config, extract, storage, store

    if not config.NOTION_TOKEN:
        return None, ""
    key = "paper-pdf:" + store.url_hash(url)
    uri = ""
    client = None
    try:
        client = store.db()
        snap = client.collection("art_cache").document(store.url_hash(key)).get()
        row = snap.to_dict() if snap.exists else {}
        uri = row.get("archive", "")
        if uri.startswith("gs://" + config.GCS_BUCKET + "/"):
            bucket = storage._bucket()
            obj = bucket.blob(uri.split("/", 3)[3])
            obj.reload()
            if obj.size <= 20 * 1024 * 1024:
                data = obj.download_as_bytes(if_generation_match=obj.generation)
                if data.startswith(b"%PDF"):
                    return data, uri
    except Exception:
        pass
    ident = extract.arxiv_id(url)
    urls = (["https://arxiv.org/pdf/" + ident] if ident else []) + list(candidates)
    started = time.monotonic()
    for pdf_url in list(dict.fromkeys(urls))[:2]:
        if time.monotonic() - started > 12:
            break
        try:
            data = download(pdf_url, limit=20 * 1024 * 1024, timeout=6)
            if not data.startswith(b"%PDF"):
                continue
            uri = storage.archive(
                data, store.url_hash(url) + "-paper.pdf", "application/pdf"
            )
            if uri and client:
                try:
                    client.collection("art_cache").document(store.url_hash(key)).set(
                        {"archive": uri, "at": time.time()}
                    )
                except Exception:
                    pass
            return data, uri
        except Exception:
            continue
    return None, ""
