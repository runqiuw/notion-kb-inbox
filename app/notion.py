"""Notion writes.

Two rules from the plan that this module enforces:
  * automation only ever writes blocks it created (section 24). Re-processing
    appends a new collapsed block; it never rewrites what is already there.
  * AI text and user text are visually separated (section 7).
"""
import copy
import json
import logging
import time
from typing import Any, Iterable

import httpx

from . import config
from .reading import card_metadata

log = logging.getLogger(__name__)
API = "https://api.notion.com/v1"
CHUNK = 1900          # Notion hard limit is 2000 chars per rich_text element
MAX_CHILDREN = 100    # Notion hard limit per blocks request
MAX_REQUEST_BLOCKS = 900  # Includes every descendant; leave room below 1000.
MAX_REQUEST_BYTES = 400_000  # Conservative margin below the 500KB API limit.
SOURCE_ARROW = " ↗\ufe0e"  # Text presentation, not the large platform emoji.
_topics_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}



def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.NOTION_TOKEN}",
        "Notion-Version": config.NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    from . import work_budget
    r = httpx.request(method, f"{API}{path}", json=payload,
                      headers=_headers(), timeout=work_budget.timeout(40.0, reserve=5))
    if r.status_code >= 400:
        raise RuntimeError(f"notion {method} {path} -> {r.status_code} {r.text[:400]}")
    return r.json()


# ---------- block builders ----------

def _rt(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text[:CHUNK]}}]


def _split(text: str) -> list[str]:
    """Split on paragraph boundaries, then hard-wrap anything still too long."""
    out: list[str] = []
    for para in (text or "").split("\n"):
        para = para.rstrip()
        if not para:
            continue
        while len(para) > CHUNK:
            out.append(para[:CHUNK])
            para = para[CHUNK:]
        out.append(para)
    return out


def paragraphs(text: str) -> list[dict]:
    return [{"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": _rt(p)}} for p in _split(text)]


def heading(text: str, level: int = 2) -> dict:
    if level not in (1, 2, 3):
        raise ValueError("Notion heading level must be 1, 2 or 3")
    kind = f"heading_{level}"
    return {"object": "block", "type": kind,
            kind: {"rich_text": _rt(text)}}


def callout(text: str, emoji: str = "🤖", color: str = "gray_background") -> dict:
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": _rt(text), "icon": {"emoji": emoji},
                        "color": color}}


def bookmark(url: str) -> dict:
    """An explicit source preview, used in the chat archive's source section.

    Material pages instead link only the arrow at the end of their title.
    """
    return {"object": "block", "type": "bookmark", "bookmark": {"url": url}}


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _rt_link(text: str, url: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text[:CHUNK], "link": {"url": url}}}]


def link_callout(prefix: str, label: str, url: str, emoji: str = "✍️",
                 color: str = "yellow_background") -> dict:
    """A callout ending in a link. Used for the entry points that have to carry
    a page id, so they can only be built once the page exists."""
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": _rt(prefix) + _rt_link(label, url),
                        "icon": {"emoji": emoji}, "color": color}}


def synced_block(children: list[dict]) -> dict:
    """An original synced block: the one copy of a note's body.

    A reference to it can then be shown on the material page, so a thought can
    be read where it was had. Not a copy -- there is a single body, and editing
    it in either place changes both.
    """
    return {"object": "block", "type": "synced_block",
            "synced_block": {"synced_from": None, "children": children}}


def synced_reference(block_id: str) -> dict:
    return {"object": "block", "type": "synced_block",
            "synced_block": {"synced_from": {"block_id": block_id}}}


def toggle(summary: str, children: list[dict]) -> dict:
    return {"object": "block", "type": "toggle",
            "toggle": {"rich_text": _rt(summary), "children": children}}


def toggle_heading(text: str, children: list[dict], color: str = "default") -> dict:
    """A heading that folds its children away by default -- same look as
    `heading`, but for sections that are worth keeping, not worth scanning
    every time (e.g. 来源与定位)."""
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rt(text), "is_toggleable": True,
                          "color": color, "children": children}}


def code(text: str, language: str = "plain text") -> list[dict]:
    return [{"object": "block", "type": "code",
             "code": {"rich_text": _rt(c), "language": language}}
            for c in _split(text)]


def bullets(items: Iterable[str]) -> list[dict]:
    return [{"object": "block", "type": "bulleted_list_item",
             "bulleted_list_item": {"rich_text": _rt(i)}} for i in items if i]


def _json_bytes(payload: dict) -> int:
    # Escaped JSON is a conservative upper bound on the UTF-8 transport size.
    return len(json.dumps(payload, ensure_ascii=True).encode("utf-8"))


def normalize_blocks(blocks: list[dict]) -> list[dict]:
    """Split oversized rich-text arrays without changing source text or styles."""
    out = []
    for original in blocks:
        block = copy.deepcopy(original)
        kind = block["type"]
        body = block[kind]
        children = body.pop("children", None)
        rich = body.get("rich_text", [])
        if kind in {"paragraph", "code", "bulleted_list_item", "numbered_list_item"}:
            elements = []
            for element in rich:
                if element.get("type", "text") == "text":
                    content = element["text"]["content"]
                    for start in range(0, max(1, len(content)), CHUNK):
                        piece = copy.deepcopy(element)
                        piece["text"]["content"] = content[start:start + CHUNK]
                        elements.append(piece)
                else:
                    elements.append(element)
            groups, group = [], []
            for element in elements:
                if group and (len(group) >= 50 or _json_bytes({"rich_text": [*group, element]}) > 100_000):
                    groups.append(group)
                    group = []
                group.append(element)
            groups.append(group)
            for group in groups:
                piece = copy.deepcopy(block)
                piece[kind]["rich_text"] = group
                out.append(piece)
        else:
            out.append(block)
        if children is not None:
            out[-1][kind]["children"] = normalize_blocks(children)
    return out


def _tree_stats(blocks: list[dict], depth: int = 1) -> tuple[int, bool]:
    count = len(blocks)
    valid = len(blocks) <= MAX_CHILDREN and (not blocks or depth <= 2)
    for block in blocks:
        children = block[block["type"]].get("children", [])
        n, ok = _tree_stats(children, depth + 1)
        count += n
        valid = valid and ok
    return count, valid


def block_batches(blocks: list[dict], base: dict | None = None):
    """Pack by total descendants, encoded bytes, arrays and nesting depth.

    Oversized subtrees are created as empty containers, then populated using
    their returned IDs. Folded text consumes the same budget as visible text.
    """
    batch, deferred = [], {}
    base = base or {}
    for original in normalize_blocks(blocks):
        block = copy.deepcopy(original)
        body = block[block["type"]]
        count, valid = _tree_stats([block])
        later = None
        if not valid or count > MAX_REQUEST_BLOCKS or _json_bytes({**base, "children": [block]}) > MAX_REQUEST_BYTES:
            later = body.pop("children", None)
            if later is None or _json_bytes({**base, "children": [block]}) > MAX_REQUEST_BYTES:
                raise ValueError("Notion 单个块或页面属性超过安全请求大小")
        candidate = [*batch, block]
        count, valid = _tree_stats(candidate)
        if batch and (not valid or count > MAX_REQUEST_BLOCKS or _json_bytes({**base, "children": candidate}) > MAX_REQUEST_BYTES):
            yield batch, deferred
            batch, deferred, base = [], {}, {}
        if later:
            deferred[len(batch)] = later
        batch.append(block)
    if batch:
        yield batch, deferred


def _fill_deferred(result: dict, deferred: dict, *, page: bool = False) -> None:
    if not deferred:
        return
    children = list_children(result["id"]) if page else result["results"]
    for index, blocks in deferred.items():
        append_blocks(children[index]["id"], blocks)


def _create_with_blocks(payload: dict, blocks: list[dict]) -> dict:
    batches = iter(block_batches(blocks, payload))
    first, deferred = next(batches, ([], {}))
    page = _create_tagged_page({**payload, "children": first})
    _fill_deferred(page, deferred, page=True)
    for children, deferred in batches:
        result = _call("PATCH", f"/blocks/{page['id']}/children", {"children": children})
        _fill_deferred(result, deferred)
    return page


# ---------- property builders ----------

def _title(v: str) -> dict:
    value = v or "未命名"
    return {"title": [{"text": {"content": value[i:i + CHUNK]}}
                      for i in range(0, len(value), CHUNK)]}


def material_title(title: str, url: str | None) -> dict:
    """Keep the source title plain; only the final arrow opens the source."""
    value = _title(title)
    if url:
        value["title"].extend(_rt_link(SOURCE_ARROW, url))
    return value


def _select(v: str | None) -> dict:
    return {"select": {"name": v} if v else None}


def _multi(vs: list[str]) -> dict:
    return {"multi_select": [{"name": v} for v in (vs or [])[:5]]}


def _url(v: str | None) -> dict:
    return {"url": v or None}


def _rel(ids: list[str]) -> dict:
    return {"relation": [{"id": i} for i in ids]}


# ---------- page operations ----------

def known_topics() -> list[str]:
    """Read both live tag vocabularies; a short cache avoids per-item API reads."""
    if not config.NOTION_TOKEN:
        return []
    key = (config.NOTION_MATERIAL_DB, config.NOTION_THOUGHT_DB)
    now = time.monotonic()
    cached = _topics_cache.get(key)
    if cached and now - cached[0] < 300:
        return list(cached[1])
    names: list[str] = []
    for db in key:
        if not db:
            continue
        try:
            schema = _call("GET", f"/databases/{db}")
            options = schema.get("properties", {}).get("主题", {}).get("multi_select", {}).get("options", [])
            names.extend(o["name"] for o in options if o.get("name"))
        except Exception:
            log.warning("could not refresh tag vocabulary")
    names = list(dict.fromkeys(names))
    if not names and cached:
        return list(cached[1])
    _topics_cache[key] = (now, names)
    return names


def related_topics(ids: list[str]) -> list[str]:
    """Inherit tags at note creation only; never overwrite later human edits."""
    if not config.NOTION_TOKEN:
        return []
    names: list[str] = []
    for page_id in ids:
        try:
            page = _call("GET", f"/pages/{page_id}")
            names.extend(t["name"] for t in page.get("properties", {}).get("主题", {}).get("multi_select", []))
        except Exception:
            log.warning("could not inherit material tags")
    return list(dict.fromkeys(names))[:5]

def paper_props(authors: list[str], published: str, venue: str,
                citations: int | None) -> dict:
    """Bibliographic fields, as Notion properties rather than page text so they
    can be filtered and sorted. Every one is optional: arXiv does not carry a
    venue for a preprint, and Semantic Scholar rate-limits anonymous callers
    hard enough that an empty citation count is the common case, not a fault."""
    props: dict[str, Any] = {}
    if authors:
        props["作者"] = {"rich_text": _rt("、".join(authors[:12]))}
    if published:
        props["发表时间"] = {"date": {"start": published}}
    if venue:
        props["会议/期刊"] = {"rich_text": _rt(venue)}
    if citations is not None:
        props["引用数"] = {"number": citations}
    return props


def _create_tagged_page(payload: dict) -> dict:
    from . import tag_store
    with tag_store.write_guard():
        values = [t["name"] for t in payload["properties"]["主题"]["multi_select"]]
        # Re-read the latest version immediately before the only page create.
        values = tag_store.normalize(values, writing=True)
        payload["properties"]["主题"] = _multi(values)
        return _call("POST", "/pages", payload)


def create_material(*, title: str, material_type: str, platform: str,
                    url: str | None, topics: list[str], processing: str,
                    reading: str = "待读", blocks: list[dict],
                    extra_props: dict | None = None,
                    thumbnail: dict | None = None, icon: dict | None = None) -> dict:
    props: dict[str, Any] = {
        "标题": material_title(title, url),
        "材料类型": _select(material_type),
        "来源平台": _select(platform),
        "阅读状态": _select(reading),
        "处理状态": _select(processing),
        "主题": _multi(topics),
        "样例来源": _select("用户提供"),
    }
    if url:
        props["原始链接"] = _url(url)
    props.update(extra_props or {})
    # The pipeline applies content artwork or a category fallback after its
    # checkpoint. If uploads are unavailable, the base page keeps its icon;
    # unrelated stock photos/gradients must not become a permanent fallback.
    if thumbnail:
        props["缩略图"] = {"files": [thumbnail]}
    for name, value in card_metadata(blocks, material_type).items():
        if value:
            props[name] = {"rich_text": _rt(value)}
    payload: dict[str, Any] = {
        "parent": {"database_id": config.NOTION_MATERIAL_DB},
        "properties": props,
    }
    from .thumbnails import EMOJI
    payload["icon"] = {"type": "emoji", "emoji": EMOJI.get(material_type, "📎")}
    page = _create_with_blocks(payload, blocks)
    if icon:
        decorate_page(page["id"], icon=icon)
    return page


def create_thought(*, title: str, note_type: str | None, topics: list[str],
                   related: list[str], blocks: list[dict],
                   status: str = "待展开", sync_body: bool = False,
                   icon: dict | None = None) -> dict:
    if not topics and related:
        topics = related_topics(related)
    props: dict[str, Any] = {
        "标题": _title(title),
        "类型": _select(note_type),
        "笔记状态": _select(status),
        "主题": _multi(topics),
        "样例来源": _select("用户提供"),
    }
    if related:
        props["关联资料"] = _rel(related)
    # A note about a material is mirrored onto that material's page, so the
    # thought can be read next to what prompted it instead of one click away.
    # The body goes in a synced block to make that one body rather than two
    # copies that drift apart the first time either side is edited.
    wrapped = bool(blocks and (related or sync_body))
    mirrored = bool(related and wrapped)
    from .thumbnails import EMOJI
    page = _create_with_blocks({
        "parent": {"database_id": config.NOTION_THOUGHT_DB},
        "properties": props,
        "icon": {"type": "emoji", "emoji": EMOJI.get(note_type, "📝")},
    }, [synced_block(blocks)] if wrapped else blocks)
    # Cosmetic failure cannot cause a second page. Retrying an icon PATCH on a
    # known ID is safe, while retrying an uncertain create is not.
    try:
        from .thumbnails import note_icon
        chosen_icon = icon or note_icon(note_type)
        if chosen_icon != page.get("icon", {"type": "emoji", "emoji": EMOJI.get(note_type, "📝")}):
            decorate_page(page["id"], icon=chosen_icon)
    except Exception:
        log.info("note icon unavailable")
    container = page["id"]
    # A short staged note needs no body lookup yet. Return its page ID so the
    # caller can checkpoint it before any later read/link request might fail.
    if wrapped and (mirrored or len(blocks) > MAX_CHILDREN):
        container, _ = body_location(page)
        if container == page["id"]:
            # The wrapper is not where it should be. Mirroring now would point
            # the reference at a page id, so skip it: an unmirrored note is
            # still a correct note.
            log.warning("note %s has no synced block; not mirroring", page["id"])
            mirrored = False
    for material in related if mirrored else []:
        # The note itself is already saved; a mirror that fails is a missing
        # convenience, not a lost thought. The divider is what keeps a note from
        # reading as one more paragraph of the material it sits under.
        try:
            append_blocks(material, [divider(), synced_reference(container)])
        except Exception as e:
            log.warning("could not mirror the note onto %s: %s", material, e)
    return page


def decorate_page(page_id: str, *, thumbnail: dict | None = None,
                  icon: dict | None = None) -> None:
    """Independent optional PATCHes, never retried by creating another page."""
    if thumbnail:
        try:
            update_properties(page_id, {"缩略图": {"files": [thumbnail]}})
        except Exception:
            log.info("thumbnail unavailable for %s", page_id)
    if icon:
        try:
            _call("PATCH", f"/pages/{page_id}", {"icon": icon})
        except Exception:
            # A definitive default icon works even when a file upload expired.
            # Unknown PATCH outcomes are also safe to overwrite on this same ID.
            try: _call("PATCH", f"/pages/{page_id}", {"icon": {"type": "emoji", "emoji": "📎"}})
            except Exception: log.info("icon unavailable for %s", page_id)


def append_blocks(page_id: str, blocks: list[dict]) -> None:
    for children, deferred in block_batches(blocks):
        result = _call("PATCH", f"/blocks/{page_id}/children", {"children": children})
        _fill_deferred(result, deferred)


def update_properties(page_id: str, props: dict) -> dict:
    return _call("PATCH", f"/pages/{page_id}", {"properties": props})


def set_processing(page_id: str, status: str) -> None:
    update_properties(page_id, {"处理状态": _select(status)})


def link_thought_to_material(thought_id: str, material_ids: list[str]) -> None:
    update_properties(thought_id, {"关联资料": _rel(material_ids)})
    container = _ensure_synced_note_body(thought_id)
    for material in material_ids:
        children = list_children(material)
        # A request can be stored by Notion and still lose its HTTP response.
        # /retry must observe an existing reference rather than append another.
        if any(b.get("type") == "synced_block" and
               (b["synced_block"].get("synced_from") or {}).get("block_id") == container
               for b in children):
            continue
        blocks = []
        if not any(b.get("type") == "heading_2" and
                   "".join(t.get("text", {}).get("content", t.get("plain_text", ""))
                           for t in b["heading_2"].get("rich_text", [])) == "我的想法"
                   for b in children):
            blocks.append(heading("我的想法"))
        append_blocks(material, [*blocks, divider(), synced_reference(container)])


def _paragraph_copy(block: dict) -> dict:
    """Writable paragraph payload, preserving formatting and inline links."""
    body = block["paragraph"]
    rich = [{k: v for k, v in item.items() if k not in ("plain_text", "href")}
            for item in body.get("rich_text", [])]
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": rich, **({"color": body["color"]} if "color" in body else {})}}


def _ensure_synced_note_body(thought_id: str) -> str:
    """Reuse a note's shared body; wrap old staged notes' paragraphs if needed.

    Provenance headings stay in place. Only the paragraphs copied into the
    shared body are removed, and no material content is rewritten.
    """
    top = list_children(thought_id)
    originals = [b for b in top if b.get("type") == "synced_block"
                 and not b["synced_block"].get("synced_from")]
    paragraphs_ = [b for b in top if b.get("type") == "paragraph"]
    if any(b.get("has_children") for b in paragraphs_):
        raise RuntimeError("旧笔记有嵌套段落，暂不能自动迁移正文")
    if originals:
        container = originals[0]["id"]
        # Recover a legacy conversion interrupted after creating the wrapper:
        # remove only leftover paragraphs still identical to the shared body.
        copies = [_paragraph_copy(b) for b in list_children(container)
                  if b.get("type") == "paragraph"]
        old = [_paragraph_copy(b) for b in paragraphs_]
        if old and len(copies) < len(old) and copies == old[:len(copies)]:
            # The wrapper was stored, but appending its remaining paragraphs
            # failed. Finish that copy before removing any original blocks.
            append_blocks(container, old[len(copies):])
        elif not all(b in copies for b in old):
            paragraphs_ = []
    else:
        if not paragraphs_:
            raise RuntimeError("笔记没有可同步的正文")
        append_blocks(thought_id, [synced_block([_paragraph_copy(b) for b in paragraphs_])])
        container = next(b["id"] for b in list_children(thought_id)
                         if b.get("type") == "synced_block" and not b["synced_block"].get("synced_from"))
    for b in paragraphs_:
        delete_block(b["id"])
    return container


def get_page(page_id: str) -> dict | None:
    """None when the page is gone. Used by entry points that arrive with a page
    id from outside -- a stale deep link must not raise."""
    try:
        page = _call("GET", f"/pages/{page_id}")
    except Exception as e:
        log.warning("could not read page %s: %s", page_id, e)
        return None
    if page.get("archived") or page.get("in_trash"):
        return None
    return page


def page_alive(page_id: str) -> bool:
    """False when the page is gone or sitting in Notion's trash.

    Dedupe is indexed in Firestore, which has no idea the user deleted a page in
    Notion. Without this check a deleted material can never be re-saved: the bot
    keeps answering 已保存过 about a page that no longer exists.
    """
    try:
        page = _call("GET", f"/pages/{page_id}")
    except Exception as e:
        if "404" in str(e) or "not_found" in str(e):
            return False
        raise   # a network blip must not look like a deletion
    return not (page.get("archived") or page.get("in_trash"))


def find_materials(statuses: tuple[str, ...], limit: int = 50) -> list[dict]:
    """Materials whose processing never finished, oldest first.

    Oldest first so a repeated retry works through the backlog instead of
    redoing the same newest few every time.
    """
    body = {
        "filter": {"or": [{"property": "处理状态", "select": {"equals": st}}
                          for st in statuses]},
        "sorts": [{"property": "收集时间", "direction": "ascending"}],
        "page_size": min(limit, 100),
    }
    res = _call("POST", f"/databases/{config.NOTION_MATERIAL_DB}/query", body)
    return res.get("results", [])


def prop_text(page: dict, name: str) -> str:
    p = (page.get("properties") or {}).get(name) or {}
    kind = p.get("type")
    if kind == "title":
        parts = p.get("title", [])
        # The source arrow is presentation, not part of the original title fed
        # back to Telegram or enrichment. Only recognize our separate suffix.
        source = ((page.get("properties") or {}).get("原始链接") or {}).get("url")
        if parts and source:
            last = parts[-1]
            if (last.get("text", {}).get("content") in {SOURCE_ARROW, " ↗"}
                    and (last.get("text", {}).get("link") or {}).get("url") == source):
                parts = parts[:-1]
        return "".join(t.get("plain_text", t.get("text", {}).get("content", "")) for t in parts)
    if kind == "rich_text":
        return "".join(t.get("plain_text", "") for t in p.get("rich_text", []))
    if kind == "url":
        return p.get("url") or ""
    if kind == "select":
        return ((p.get("select") or {}).get("name")) or ""
    return ""


def refresh_machine_fields(page_id: str, *, title: str, material_type: str,
                           topics: list[str], processing: str,
                           overview: str = "", existing_page: dict | None = None) -> None:
    """Refresh machine title/type/status, preserving tags and reading state.

    Fill a missing preview only. The existing card and page body stay intact.
    `topics` remains accepted for compatibility; retries never replace tags.
    """
    props = {
        "标题": material_title(title, prop_text(existing_page or {}, "原始链接")),
        "材料类型": _select(material_type),
        "处理状态": _select(processing),
    }
    if overview and existing_page is not None and not prop_text(existing_page, "卡片摘要"):
        preview = card_metadata([heading("中文概览"), *paragraphs(overview)], material_type)
        if preview["卡片摘要"]:
            props["卡片摘要"] = {"rich_text": _rt(preview["卡片摘要"])}
    update_properties(page_id, props)


def page_url(page: dict) -> str:
    return page.get("url", "")


def query_database(database_id: str) -> list[dict]:
    """Every page in a database, unfiltered, paginated to the end."""
    out: list[dict] = []
    cursor = None
    while True:
        body: dict[str, Any] = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = _call("POST", f"/databases/{database_id}/query", body)
        out.extend(res.get("results", []))
        if not res.get("has_more"):
            return out
        cursor = res.get("next_cursor")


def list_children(block_id: str) -> list[dict]:
    """Every direct child of a block, paginated to the end."""
    out: list[dict] = []
    cursor = None
    while True:
        path = f"/blocks/{block_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        res = _call("GET", path)
        out.extend(res.get("results", []))
        if not res.get("has_more"):
            return out
        cursor = res.get("next_cursor")


def archive_page(page_id: str) -> None:
    """Move a page to Notion's trash. A soft delete -- recoverable from the
    workspace trash until Notion's own retention window runs out -- never a
    permanent one."""
    _call("PATCH", f"/pages/{page_id}", {"archived": True})


def delete_block(block_id: str) -> None:
    _call("DELETE", f"/blocks/{block_id}")


def update_paragraph(block_id: str, text: str) -> None:
    _call("PATCH", f"/blocks/{block_id}",
          {"paragraph": {"rich_text": _rt(text)}})


def replace_paragraphs(page_id: str, block_ids: list[str], text: str) -> list[str]:
    """Rewrite a run of paragraph blocks we previously created.

    Used only for the user's own edited message (plan section 14) -- the one
    case where replacing beats appending, because the old text is a dictation
    error the user already corrected.
    """
    chunks = _split(text) or [""]
    kept: list[str] = []
    for i, chunk in enumerate(chunks):
        if i < len(block_ids):
            update_paragraph(block_ids[i], chunk)
            kept.append(block_ids[i])
        else:
            res = _call("PATCH", f"/blocks/{page_id}/children", {
                "children": [{"object": "block", "type": "paragraph",
                              "paragraph": {"rich_text": _rt(chunk)}}]})
            kept.extend(b["id"] for b in res.get("results", []))
    for stale in block_ids[len(chunks):]:
        try:
            delete_block(stale)
        except Exception as e:
            log.warning("could not delete stale block %s: %s", stale, e)
    return kept


def body_location(page: dict) -> tuple[str, list[str]]:
    """Where a freshly created note's body lives: (container id, block ids).

    Usually the page itself. When the body was wrapped in a synced block so it
    could be mirrored onto the material, edits have to target that block's
    children instead -- aimed at the page, a corrected message would append a
    stray paragraph next to the mirror rather than fix the text inside it.
    """
    top = list_children(page["id"])
    for b in top:
        if b.get("type") == "synced_block" and not b.get("synced_block", {}).get("synced_from"):
            container = b["id"]
            return container, [p["id"] for p in list_children(container) if p.get("type") == "paragraph"]
    # Source headings/toggles are metadata, not part of the editable message.
    return page["id"], [b["id"] for b in top if b.get("type") == "paragraph"]


# ---------- file uploads ----------

def upload_file(data: bytes, filename: str, content_type: str) -> str | None:
    """Upload bytes to Notion so the original is visible in the page itself.

    Returns a file_upload id, or None. Failure is not fatal: the artifact is
    already archived in GCS, so we degrade to a text pointer rather than lose it.
    """
    from . import work_budget
    try:
        work_budget.timeout(120, reserve=5)
        created = _call("POST", "/file_uploads",
                        {"filename": filename, "content_type": content_type})
        r = httpx.post(
            created["upload_url"],
            headers={"Authorization": f"Bearer {config.NOTION_TOKEN}",
                     "Notion-Version": config.NOTION_VERSION},
            files={"file": (filename, data, content_type)},
            timeout=work_budget.timeout(120.0, reserve=5),
        )
        if r.status_code >= 400:
            log.warning("notion file upload failed: %s %s", r.status_code, r.text[:300])
            return None
        return created["id"]
    except Exception as e:
        log.warning("notion file upload error: %s", e)
        return None


def image_block(file_upload_id: str) -> dict:
    return {"object": "block", "type": "image",
            "image": {"type": "file_upload",
                      "file_upload": {"id": file_upload_id}}}


def pdf_block(file_upload_id: str) -> dict:
    return {"object": "block", "type": "pdf",
            "pdf": {"type": "file_upload",
                    "file_upload": {"id": file_upload_id}}}
