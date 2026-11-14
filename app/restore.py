"""Restore a verified snapshot into NEW Notion databases and an isolated prefix.

No production database ID is accepted as a destination. IDs, relations, shared
blocks, file pointers and Firestore mappings are rewritten to the new copies.
"""
import argparse
import base64
import copy
import hashlib
import json
import uuid
import zipfile
from pathlib import Path

import httpx
from google.cloud.firestore_v1 import _helpers
from google.cloud.firestore_v1.types import Document

from . import backup, config, notion, store

WRITABLE = {"title", "rich_text", "number", "select", "multi_select", "date", "people",
            "files", "checkbox", "url", "email", "phone_number", "relation", "status"}
BLOCKS = {"paragraph", "heading_1", "heading_2", "heading_3", "heading_4", "bulleted_list_item",
          "numbered_list_item", "to_do", "quote", "toggle", "callout", "code", "divider",
          "bookmark", "embed", "image", "pdf", "file", "audio", "video", "synced_block",
          "table_of_contents", "breadcrumb", "equation", "link_to_page", "table", "table_row"}
TITLES = {"materials": "材料", "thoughts": "笔记", "life_materials": "额外资料", "life_notes": "额外笔记",
          "wiki": "Wiki 条目", "wiki_log": "Wiki 维护记录"}
TABLE_ROWS_PER_REQUEST = 100


def walk(blocks):
    for b in blocks:
        yield b
        yield from walk(b.get("_children", []))


def page_closure(pages, seeds):
    """Keep a whole relation/synced-source component, never half a shared note."""
    by_id = {backup.key(p["page"]["id"]): p for p in pages}
    selected = {backup.key(s) for s in seeds} if seeds else set(by_id)
    if not selected <= by_id.keys():
        raise ValueError("A requested material is absent from this snapshot")
    owners = {backup.key(b["id"]): pid for pid, p in by_id.items() for b in walk(p["blocks"])}
    edges = []
    for pid, p in by_id.items():
        for prop in p["page"]["properties"].values():
            if prop["type"] == "relation":
                for rel in prop["relation"]:
                    rid = backup.key(rel["id"])
                    if rid in by_id:
                        edges.append((pid, rid))
        for b in walk(p["blocks"]):
            source = (b.get("synced_block", {}).get("synced_from") or {}).get("block_id")
            if source:
                if backup.key(source) not in owners:
                    raise ValueError("A shared-block source is outside the backup")
                edges.append((pid, owners[backup.key(source)]))
    while True:
        expanded = selected | {b for a, b in edges if a in selected} | {a for a, b in edges if b in selected}
        if expanded == selected:
            return [p for pid, p in by_id.items() if pid in selected]
        selected = expanded


def schema_properties(schema):
    out = {}
    for name, prop in schema["properties"].items():
        kind = prop["type"]
        if kind in ("relation", "rollup", "button"):
            continue
        if kind in ("select", "multi_select"):
            body = {"options": [{"name": o["name"], "color": o.get("color", "default")}
                                for o in prop[kind]["options"]]}
        elif kind == "unique_id":
            # Prefixes are unique across the workspace, not just a database.
            # Old numbers/prefixes remain in the backup and restore report.
            body = {"prefix": "R" + uuid.uuid4().hex[:8]}
        elif kind in ("number", "formula"):
            body = prop[kind]
        elif kind == "status":
            raise ValueError("Custom status schema restoration is not supported by this API version")
        else:
            body = {}
        out[name] = {kind: body}
    return out


def plain_link(item):
    """A link preview mention cannot be written by this API version. Keep its
    visible text and URL as an ordinary link; only the preview chip is lost."""
    if not (isinstance(item, dict) and item.get("type") == "mention"
            and item.get("mention", {}).get("type") == "link_mention"):
        return None
    url = item["mention"]["link_mention"].get("href") or item.get("href")
    return {"type": "text", "text": {"content": item.get("plain_text") or url, "link": {"url": url}},
            "annotations": item.get("annotations", {})}


TEXT_LIMIT = 2000  # Notion write limit per text object, in UTF-16 units


def split_text(item):
    """Reads can return longer text objects than writes accept: split them,
    keeping annotations and link on every piece."""
    content = item["text"]["content"]
    if len(content.encode("utf-16-le")) // 2 <= TEXT_LIMIT:
        return [item]
    pieces, current, units = [], [], 0
    for ch in content:
        width = len(ch.encode("utf-16-le")) // 2
        if units + width > TEXT_LIMIT:
            pieces.append("".join(current))
            current, units = [], 0
        current.append(ch)
        units += width
    pieces.append("".join(current))
    return [{**item, "text": {**item["text"], "content": p}} for p in pieces]


def merge_text(items):
    """Compare rich text independent of how Notion split it into objects."""
    out = []
    for item in items:
        prev = out[-1] if out else None
        if (isinstance(item, dict) and isinstance(prev, dict)
                and item.get("type") == "text" == prev.get("type") and "text" in item and "text" in prev
                and item.get("annotations") == prev.get("annotations")
                and item["text"].get("link") == prev["text"].get("link")):
            prev["text"] = {**prev["text"], "content": prev["text"]["content"] + item["text"]["content"]}
        else:
            out.append(copy.deepcopy(item))
    return out


def verify_shared_edit(api, reference, source):
    """Briefly edit a TEST copy, read through its mirror, then restore it."""
    original = next((b for b in api.children(source) if b["type"] == "paragraph"), None)
    if not original:
        raise ValueError("Shared edit drill needs a paragraph in the restored note")
    bid = original["id"]
    rich = [{k: v for k, v in r.items() if k not in ("plain_text", "href")}
            for r in original["paragraph"]["rich_text"]]
    marker = "备份恢复演练：同步编辑验证"
    try:
        api.call("PATCH", "/blocks/" + bid, {"paragraph": {"rich_text": [{"text": {"content": marker}}]}})
        visible = next((b for b in api.children(reference) if b["id"] == bid), None)
        if not visible or "".join(r.get("text", {}).get("content", "") for r in visible["paragraph"]["rich_text"]) != marker:
            raise RuntimeError("Editing the restored source did not update its material mirror")
    finally:
        api.call("PATCH", "/blocks/" + bid, {"paragraph": {
            "rich_text": rich, "color": original["paragraph"].get("color", "default")}})
    visible = next((b for b in api.children(reference) if b["id"] == bid), None)
    restored = [{k: v for k, v in r.items() if k not in ("plain_text", "href")}
                for r in (visible or {}).get("paragraph", {}).get("rich_text", [])]
    if restored != rich:
        raise RuntimeError("Shared edit drill failed to restore the original paragraph")
    return True


class Restorer:
    def __init__(self, archive, api, fs, gcs, bucket, report_path, resume=False):
        self.manifest = backup.verify_archive(archive)
        self.archive = zipfile.ZipFile(archive)
        self.snapshot = json.loads(self.archive.read("notion.json"))
        self.api, self.fs, self.gcs = api, fs, gcs
        self.bucket = bucket
        if bucket == self.manifest["source"]["bucket"]:
            raise ValueError("Restore files must use a separate bucket")
        self.report_path = Path(report_path)
        if self.report_path.exists() and not resume:
            raise ValueError("Use a fresh report path for each isolated restore")
        self.ids, self.files, self.pointers = {}, {}, {}
        self.report = {"status": "in_progress", "started_at": backup.now(),
                       "source_completed_at": self.manifest["completed_at"], "ids": self.ids,
                       "checks": {}, "read_only_metadata": {}, "omitted_schema": {}}
        self.resume = resume
        if resume:
            self.report = json.loads(self.report_path.read_text())
            if self.report["status"] != "in_progress" or self.report["source_completed_at"] != self.manifest["completed_at"]:
                raise ValueError("Resume requires the same snapshot and an unfinished isolated restore")
            self.ids.update(self.report["ids"])
            self.report["ids"] = self.ids

    def journal(self):
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_bytes(backup.canonical(self.report))

    def mapped(self, value):
        return self.ids.get(backup.key(value), value)

    def rewrite(self, value):
        if isinstance(value, str):
            for old, new in self.pointers.items():
                value = value.replace(old, new)
            # Structured IDs and page URLs/deep links share the same old ID.
            if backup.key(value) in self.ids:
                return self.mapped(value)
            for old, new in self.ids.items():
                value = value.replace(old, backup.key(new))
                dashed = f"{old[:8]}-{old[8:12]}-{old[12:16]}-{old[16:20]}-{old[20:]}"
                value = value.replace(dashed, new)
            return value
        if isinstance(value, list):
            return [self.rewrite(v) for v in value]
        if isinstance(value, dict):
            return {k: self.rewrite(v) for k, v in value.items()}
        return value

    def clean(self, value):
        if isinstance(value, list):
            out = []
            for v in value:
                v = self.clean(v)
                out.extend(split_text(v) if isinstance(v, dict) and v.get("type") == "text" and "text" in v else [v])
            return out
        if not isinstance(value, dict):
            return self.rewrite(value)
        link = plain_link(value)
        if link:
            self.report["checks"]["link_mentions_as_links"] = self.report["checks"].get("link_mentions_as_links", 0) + 1
            return self.clean(link)
        if "_backup_asset" in value:
            asset = value["_backup_asset"]
            digest = asset["sha256"]
            if digest not in self.files:
                data = self.archive.read(asset["entry"])
                created = self.api.call("POST", "/file_uploads", {
                    "filename": asset["filename"], "content_type": asset["content_type"].split(";", 1)[0]})
                r = httpx.post(created["upload_url"],
                               headers={"Authorization": "Bearer " + config.NOTION_TOKEN,
                                        "Notion-Version": config.NOTION_VERSION},
                               files={"file": (asset["filename"], data, asset["content_type"])}, timeout=120)
                if r.status_code != 200:
                    raise RuntimeError(f"Restored attachment upload: HTTP {r.status_code}")
                self.files[digest] = created["id"]
            return {"type": "file_upload", "file_upload": {"id": self.files[digest]},
                    **({"name": value["name"]} if "name" in value else {}),
                    **({"caption": self.clean(value["caption"])} if value.get("caption") else {})}
        # list_format is returned on read but rejected on write by this API
        # version. Only the default renders the same without it; verify()
        # still compares it, so a non-default format fails loudly.
        return {k: self.clean(v) for k, v in value.items()
                if k not in ("plain_text", "href", "_backup_asset", "expiry_time", "list_format")
                and not (k == "icon" and v is None)}

    def properties(self, page, relations=False):
        out = {}
        for name, p in page["properties"].items():
            kind = p["type"]
            if kind not in WRITABLE or (kind == "relation") != relations:
                continue
            body = p[kind]
            if kind in ("select", "status"):
                body = {"name": body["name"]} if body else None
            elif kind == "multi_select":
                body = [{"name": v["name"]} for v in body]
            elif kind == "relation":
                restored_ids = {backup.key(v) for v in self.ids.values()}
                missing = [r["id"] for r in body if backup.key(r["id"]) not in self.ids
                           and backup.key(r["id"]) not in restored_ids]
                if missing:
                    raise ValueError("Relation target absent from restored group")
                body = [{"id": self.mapped(r["id"])} for r in body]
            elif kind == "people":
                body = [{"id": u["id"]} for u in body]
            out[name] = {kind: self.clean(body)}
        return out

    def append(self, parent, blocks):
        # Shallow creation lets us journal every returned ID, then restore
        # arbitrary nesting and >100 children without truncating the tree.
        for i in range(0, len(blocks), 100):
            batch = blocks[i:i + 100]
            missing = [b for b in batch if backup.key(b["id"]) not in self.ids]
            payload = []
            for b in missing:
                kind = b["type"]
                payload.append({"object": "block", "type": kind, kind: self.clean(b[kind])})
            # Restoration already writes shallow blocks, but 100 large source
            # blocks can still exceed the JSON byte limit. Journal each batch.
            packed, originals = [], []
            def flush():
                if not packed:
                    return
                results = self.api.call("PATCH", f"/blocks/{parent}/children", {"children": packed})["results"]
                if len(results) != len(originals):
                    raise RuntimeError("Notion returned an incomplete restored block batch")
                for old, new in zip(originals, results):
                    self.ids[backup.key(old["id"])] = new["id"]
                self.journal()
                packed.clear()
                originals.clear()
            for old, block in zip(missing, payload):
                if old["type"] == "table":
                    flush()
                    self.create_table(parent, old, block)
                    continue
                if notion._json_bytes({"children": [block]}) > notion.MAX_REQUEST_BYTES:
                    raise ValueError("备份中的单个块超过 Notion 安全请求大小，不能静默截断恢复")
                if packed and notion._json_bytes({"children": [*packed, block]}) > notion.MAX_REQUEST_BYTES:
                    flush()
                packed.append(block)
                originals.append(old)
            flush()
            for old in batch:
                if old.get("_children"):
                    self.append(self.mapped(old["id"]), old["_children"])

    def create_table(self, parent, old, block):
        """Notion only creates a table together with its rows. Send as many rows
        as fit in one request; append() adds the rest to the new table."""
        rows, inline = old.get("_children", []), []
        for row in rows[:TABLE_ROWS_PER_REQUEST]:
            entry = {"object": "block", "type": "table_row", "table_row": self.clean(row["table_row"])}
            trial = {**block, "table": {**block["table"], "children": [*inline, entry]}}
            if inline and notion._json_bytes({"children": [trial]}) > notion.MAX_REQUEST_BYTES:
                break
            inline.append(entry)
        if not inline:
            raise ValueError("A table without rows cannot be restored")
        table = {**block, "table": {**block["table"], "children": inline}}
        if notion._json_bytes({"children": [table]}) > notion.MAX_REQUEST_BYTES:
            raise ValueError("备份中的单个表格行超过 Notion 安全请求大小，不能静默截断恢复")
        created = self.api.call("PATCH", f"/blocks/{parent}/children", {"children": [table]})["results"]
        if len(created) != 1:
            raise RuntimeError("Notion returned an incomplete restored table")
        new_rows = self.api.children(created[0]["id"])
        if len(new_rows) != len(inline):
            raise RuntimeError("Notion restored a different number of table rows")
        self.ids[backup.key(old["id"])] = created[0]["id"]
        for o, n in zip(rows, new_rows):
            self.ids[backup.key(o["id"])] = n["id"]
        self.journal()

    def restore(self, parent, seeds=()):
        selected = page_closure(self.snapshot["pages"], seeds)
        selected_ids = [p["page"]["id"] for p in selected]
        if self.report.get("selected_pages") and self.report["selected_pages"] != selected_ids:
            raise ValueError("Resume cannot change the restored group")
        self.report["selected_pages"] = selected_ids
        mirrors = 0
        for p in selected:
            for b in walk(p["blocks"]):
                if b["type"] not in BLOCKS:
                    raise ValueError("Unsupported block requires explicit recovery: " + b["type"])
                mirrors += bool((b.get("synced_block", {}).get("synced_from") or {}).get("block_id"))
        self.report["checks"]["synced_mirrors_in_group"] = mirrors
        # Validate schemas before creating any destination resources.
        schema = {k: schema_properties(v) for k, v in self.snapshot["databases"].items()}
        root = self.api.call("GET", "/pages/" + self.report["root_id"]) if self.report.get("root_id") else self.api.call("POST", "/pages", {"parent": {"page_id": parent},
                    "properties": {"title": {"title": [{"text": {"content": "KB Bot 备份恢复演练 " + backup.now()[:10]}}]}},
                    "children": [{"object": "block", "type": "paragraph", "paragraph": {
                        "rich_text": [{"text": {"content": "独立测试副本；由备份还原，正文、附件、关联和同步块均使用测试副本。"}}]}}]})
        self.report["root_id"], self.report["root_url"] = root["id"], root["url"]
        self.report["run_id"] = run_id = backup.key(root["id"])
        self.report["gcs_prefix"] = prefix = f"restores/{run_id}/"
        self.journal()
        if root.get("archived") or root.get("in_trash"):
            raise ValueError("Restore test root is archived")
        dbs = self.report.setdefault("databases", {})
        for kind, original in self.snapshot["databases"].items():
            if backup.key(original["id"]) in self.ids:
                dbs[kind] = self.mapped(original["id"])
                if backup.key(dbs[kind]) == backup.key(original["id"]):
                    raise ValueError("Production database cannot be a restore target")
                current = self.api.call("GET", "/databases/" + dbs[kind])
                if backup.key(current.get("parent", {}).get("page_id", "")) != backup.key(root["id"]):
                    raise ValueError("Resumed database is outside the isolated root")
                continue
            db = self.api.call("POST", "/databases", {"parent": {"page_id": root["id"]},
                               "title": [{"text": {"content": TITLES.get(kind, kind) + "（恢复测试）"}}],
                               "properties": schema[kind]})
            dbs[kind] = db["id"]
            self.ids[backup.key(original["id"])] = db["id"]
            self.report["omitted_schema"][kind] = [n for n, v in original["properties"].items() if v["type"] == "button"]
            self.journal()
        # Create only one side of dual relations; Notion creates the reciprocal.
        for kind, original in self.snapshot["databases"].items():
            for name, prop in original["properties"].items():
                if prop["type"] != "relation":
                    continue
                live = self.api.call("GET", "/databases/" + dbs[kind])
                if name in live["properties"]:
                    continue
                rel = prop["relation"]
                if backup.key(rel["database_id"]) not in self.ids:
                    raise ValueError("Relation schema points outside the backup")
                body = {"database_id": self.mapped(rel["database_id"])}
                if rel.get("type") == "dual_property":
                    body["dual_property"] = {"synced_property_name": rel["dual_property"]["synced_property_name"]}
                else:
                    body["single_property"] = {}
                self.api.call("PATCH", "/databases/" + dbs[kind], {"properties": {name: {"relation": body}}})
        for kind, original in self.snapshot["databases"].items():
            for name, prop in original["properties"].items():
                if prop["type"] == "rollup":
                    r = prop["rollup"]
                    self.api.call("PATCH", "/databases/" + dbs[kind], {"properties": {name: {"rollup": {
                        "relation_property_name": r["relation_property_name"], "rollup_property_name": r["rollup_property_name"],
                        "function": r["function"]}}}})
        # GCS bytes are copied from the ZIP, never from the live source bucket.
        objects = json.loads(self.archive.read("gcs.json"))
        for obj in objects:
            target = prefix + obj["name"]
            blob = self.gcs.bucket(self.bucket).blob(target)
            if self.resume and blob.exists():
                if hashlib.sha256(blob.download_as_bytes()).hexdigest() != obj["asset"]["sha256"]:
                    raise RuntimeError("Existing isolated GCS copy differs from backup")
                self.pointers[f"gs://{self.manifest['source']['bucket']}/{obj['name']}"] = f"gs://{self.bucket}/{target}"
                continue
            blob.metadata = obj["metadata"]
            blob.upload_from_string(self.archive.read(obj["asset"]["entry"]),
                                    content_type=obj["content_type"], if_generation_match=0)
            self.pointers[f"gs://{self.manifest['source']['bucket']}/{obj['name']}"] = f"gs://{self.bucket}/{target}"
        for p in selected:
            old = p["page"]
            if backup.key(old["id"]) in self.ids:
                current = self.api.call("GET", "/pages/" + self.mapped(old["id"]))
                if backup.key(current["parent"].get("database_id", "")) != backup.key(dbs[p["kind"]]):
                    raise ValueError("Resumed page is outside the isolated database")
                continue
            payload = {"parent": {"database_id": dbs[p["kind"]]}, "properties": self.properties(old)}
            for name in ("icon", "cover"):
                if old.get(name):
                    payload[name] = self.clean(old[name])
            new = self.api.call("POST", "/pages", payload)
            self.ids[backup.key(old["id"])] = new["id"]
            self.report["read_only_metadata"][old["id"]] = {
                "created_time": old.get("created_time"), "last_edited_time": old.get("last_edited_time"),
                "properties": {n: v for n, v in old["properties"].items() if v["type"] not in WRITABLE}}
            self.journal()
        pending = list(selected)
        while pending:
            ready = [p for p in pending if all(not (source := (b.get("synced_block", {}).get("synced_from") or {}).get("block_id"))
                          or backup.key(source) in self.ids for b in walk(p["blocks"]))]
            if not ready:
                raise ValueError("Unresolved shared-block dependency")
            for p in ready:
                self.append(self.mapped(p["page"]["id"]), p["blocks"])
                pending.remove(p)
        for p in selected:
            props = self.properties(p["page"], relations=True)
            if props:
                self.api.call("PATCH", "/pages/" + self.mapped(p["page"]["id"]), {"properties": props})
        self.restore_mappings(selected, run_id)
        self.report["databases"], self.report["selected_pages"] = dbs, [p["page"]["id"] for p in selected]
        self.verify(selected, objects, prefix)
        self.recover_originals(selected, objects)
        for p in selected:
            if p["kind"] == "materials":
                mirror = next((b for b in self.api.children(self.mapped(p["page"]["id"]))
                               if (b.get("synced_block", {}).get("synced_from") or {}).get("block_id")), None)
                if mirror:
                    source = mirror["synced_block"]["synced_from"]["block_id"]
                    if source not in self.ids.values():
                        raise ValueError("Shared edit probe cannot target a production source")
                    self.report["checks"]["shared_edit_verified"] = verify_shared_edit(self.api, mirror["id"], source)
                    break
        self.report["status"], self.report["completed_at"] = "verified", backup.now()
        self.journal()
        return self.report

    def recover_originals(self, selected, objects):
        """Historical reimports removed media blocks, but kept GCS originals.

        Reattach only byte archives with the bot's exact URL-hash naming scheme;
        unidentifiable files remain in the isolated GCS copy, never guessed.
        """
        recovered = []
        for p in selected:
            url = p["page"]["properties"].get("原始链接", {}).get("url")
            if p["kind"] != "materials" or not url or any(
                    b["type"] in ("image", "pdf", "file", "audio", "video") for b in walk(p["blocks"])):
                continue
            prefix = store.url_hash(url) + "-"
            assets, digests = [], set()
            for obj in objects:
                filename = obj["name"].rsplit("/", 1)[-1]
                if (filename.startswith(prefix) and Path(filename).stem[len(prefix):].isdigit() and
                        (obj.get("content_type") or "").startswith(("image/", "application/pdf")) and
                        obj["asset"]["sha256"] not in digests):
                    assets.append(obj)
                    digests.add(obj["asset"]["sha256"])
            if not assets:
                continue
            children = []
            for obj in assets:
                asset = self.clean({"_backup_asset": obj["asset"]})
                kind = "image" if obj["content_type"].startswith("image/") else "pdf"
                children.append({"object": "block", "type": kind, kind: asset})
            page_id = self.mapped(p["page"]["id"])
            res = self.api.call("PATCH", f"/blocks/{page_id}/children", {"children": [{
                "object": "block", "type": "heading_2", "heading_2": {"is_toggleable": True,
                "rich_text": [{"text": {"content": "从 GCS 原件恢复的附件"}}]}}]})
            heading_id = res["results"][0]["id"]
            self.api.call("PATCH", f"/blocks/{heading_id}/children", {"children": children})
            actual = self.api.children(heading_id)
            if len(actual) != len(assets):
                raise RuntimeError("Incomplete restored original attachment gallery")
            for block, obj in zip(actual, assets):
                kind = block["type"]
                media = block[kind]
                r = httpx.get(media[media["type"]]["url"], follow_redirects=True, timeout=120)
                if r.status_code != 200 or hashlib.sha256(r.content).hexdigest() != obj["asset"]["sha256"]:
                    raise RuntimeError("Restored original attachment bytes differ")
                recovered.append({"material": page_id, "source_object": obj["name"], "block": block["id"]})
        self.report["recovered_gcs_attachments"] = recovered
        self.report["checks"]["gcs_attachments_visible_in_notion"] = len(recovered)

    def restore_mappings(self, selected, run_id):
        page_ids = {backup.key(p["page"]["id"]) for p in selected}
        count, entries = 0, []
        decoded = []
        for doc in json.loads(self.archive.read("firestore.json")):
            wire = Document.deserialize(base64.b64decode(doc["wire"]))
            data = _helpers.decode_dict(wire.fields, self.fs)
            decoded.append((doc["path"], data))
        record_ids = {path.split("/", 1)[1] for path, data in decoded if path.startswith("records/")
                      and backup.key(data.get("notion_page_id", "")) in page_ids}
        for path, data in decoded:
            text = backup.key(json.dumps(data, default=str))
            # Only mapping documents for the restored group, in a collection
            # the production bot never reads. Raw update payloads remain in ZIP.
            tag_config = path in ("settings/tags", "settings/tag_navigation") or path.split("/", 1)[0] in ("tag_versions", "tag_operations")
            if not tag_config and (path.split("/", 1)[0] not in ("records", "urls", "staged", "pending", "media_groups") or not (
                    any(pid in text for pid in page_ids) or
                    (path.startswith("urls/") and data.get("record_id") in record_ids))):
                continue
            target = f"restore_drills/{run_id}/" + path
            updated = self.rewrite(data)
            if path.startswith("records/"):
                if updated.get("kind") == "thought":
                    self.refresh_body_mapping(updated, updated["notion_page_id"])
                if updated.get("link_thought"):
                    self.refresh_body_mapping(updated["link_thought"], updated["link_thought"]["id"])
            ref = self.fs.document(target)
            if not (self.resume and ref.get().exists):
                ref.create(updated)
            if self.fs.document(target).get().to_dict() != updated:
                raise RuntimeError("Restored Firestore mapping verification failed")
            count += 1
            entries.append(target)
        self.report["firestore_paths"] = entries
        self.report["checks"]["firestore_mappings"] = count

    def refresh_body_mapping(self, fields, page_id):
        # Historical formatting changes may already have invalidated stored
        # paragraph IDs. Resolve from the restored tree, rather than resurrect
        # a stale pointer. The original mapping stays untouched in the ZIP.
        blocks = self.api.children(page_id)
        source = next((b for b in blocks if b["type"] == "synced_block"
                       and not b["synced_block"].get("synced_from")), None)
        container = source["id"] if source else page_id
        if source:
            blocks = self.api.children(container)
        fields["body_parent_id"] = container
        fields["body_block_ids"] = [b["id"] for b in blocks if b["type"] == "paragraph"]

    def read_tree(self, parent):
        out = self.api.children(parent)
        for b in out:
            source = (b.get("synced_block", {}).get("synced_from") or {}).get("block_id")
            if b.get("has_children") and not source:
                b["_children"] = self.read_tree(b["id"])
        return out

    def block_shape(self, blocks, restored=False):
        out = []
        for b in blocks:
            kind, body = b["type"], b[b["type"]]
            # File IDs/temporary URLs differ, compare the actual downloaded bytes.
            body = copy.deepcopy(body)
            if kind in ("image", "pdf", "file", "audio", "video"):
                if restored:
                    url = body.get(body.get("type"), {}).get("url")
                    r = httpx.get(url, follow_redirects=True, timeout=120)
                    if r.status_code != 200:
                        raise RuntimeError("Restored attachment is unreadable")
                    digest = hashlib.sha256(r.content).hexdigest()
                else:
                    digest = body["_backup_asset"]["sha256"]
                body = {"bytes_sha256": digest, "caption": body.get("caption", [])}
            out.append({"type": kind, "body": self.shape_clean(body),
                        "children": self.block_shape(b.get("_children", []), restored)})
        return out

    def shape_clean(self, value):
        if isinstance(value, list):
            return merge_text([self.shape_clean(v) for v in value])
        if isinstance(value, dict):
            value = plain_link(value) or value
            # Notion omits the default list_format on read-back of new blocks.
            return {k: self.shape_clean(v) for k, v in value.items()
                    if k not in ("plain_text", "href", "_backup_asset", "expiry_time")
                    and not (k == "icon" and v is None) and not (k == "list_format" and v == "numbers")}
        return self.rewrite(value)

    def verify(self, selected, objects, prefix):
        files = syncs = metadata_files = tables = relations = 0
        kinds = {}
        for p in selected:
            kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
            relations += sum(len(v["relation"]) for v in p["page"]["properties"].values() if v["type"] == "relation")
            current = self.api.call("GET", "/pages/" + self.mapped(p["page"]["id"]))
            for name, expected in self.properties(p["page"]).items():
                if p["page"]["properties"][name]["type"] == "files":
                    continue  # File bytes checked below, uploaded IDs are new.
                actual = self.properties(current)[name]
                if actual != expected:
                    raise RuntimeError("Restored property mismatch: " + name)
            if self.properties(current, True) != self.properties(p["page"], True):
                raise RuntimeError("Restored note relation mismatch")
            metadata_files += self.verify_assets(p["page"], current)
            blocks = self.read_tree(current["id"])
            if self.block_shape(blocks, True) != self.block_shape(p["blocks"]):
                raise RuntimeError("Restored text/block structure mismatch")
            for b in walk(blocks):
                if b["type"] in ("image", "pdf", "file", "audio", "video"):
                    files += 1
                tables += b["type"] == "table"
                source = (b.get("synced_block", {}).get("synced_from") or {}).get("block_id")
                if source:
                    # Read through the mirror and source independently. Equal
                    # source IDs and visible content prove it is shared, not copied.
                    a, c = self.api.children(b["id"]), self.api.children(source)
                    if self.block_shape(a, True) != self.block_shape(c, True):
                        raise RuntimeError("Shared note display mismatch")
                    syncs += 1
        for obj in objects:
            data = self.gcs.bucket(self.bucket).blob(prefix + obj["name"]).download_as_bytes()
            if hashlib.sha256(data).hexdigest() != obj["asset"]["sha256"]:
                raise RuntimeError("Restored GCS attachment checksum mismatch")
        self.report["checks"].update(pages=len(selected), pages_by_role=kinds, tables=tables,
                                     relation_values=relations, gcs_objects=len(objects),
                                     notion_attachment_blocks=files, shared_note_references=syncs,
                                     notion_property_and_cover_files=metadata_files,
                                     text_and_properties="equal", relations="equal", attachments="sha256_equal")

    def verify_assets(self, expected, current):
        if isinstance(expected, dict):
            if "_backup_asset" in expected:
                url = current.get(current.get("type"), {}).get("url")
                if not url:
                    raise RuntimeError("Restored page file has no readable URL")
                r = httpx.get(url, follow_redirects=True, timeout=120)
                if r.status_code != 200 or hashlib.sha256(r.content).hexdigest() != expected["_backup_asset"]["sha256"]:
                    raise RuntimeError("Restored page file checksum mismatch")
                return 1
            return sum(self.verify_assets(v, current.get(k, {})) for k, v in expected.items())
        if isinstance(expected, list):
            return sum(self.verify_assets(v, current[i] if isinstance(current, list) and i < len(current) else {})
                       for i, v in enumerate(expected))
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("--local-gcloud", action="store_true")
    parser.add_argument("--project", default=config.GCP_PROJECT)
    parser.add_argument("--parent", required=True, help="Parent of a NEW isolated Notion drill page")
    parser.add_argument("--bucket", required=True, help="Non-production bucket for isolated attachment copies")
    parser.add_argument("--material", action="append", default=[])
    parser.add_argument("--report", required=True)
    parser.add_argument("--resume", action="store_true", help="Continue the same unfinished isolated drill")
    args = parser.parse_args()
    api, fs, gcs = backup.clients(args.local_gcloud, args.project)
    restorer = Restorer(args.archive, api, fs, gcs, args.bucket, args.report, args.resume)
    report = restorer.restore(args.parent, args.material)
    print(json.dumps({k: report[k] for k in ("status", "root_url", "run_id", "checks")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
