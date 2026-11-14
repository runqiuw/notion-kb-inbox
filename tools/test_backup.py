"""Offline round-trip: nested/long bodies, binary files, typed state and new IDs."""
import base64
import copy
import json
import os
import sys
import tempfile
import unittest
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import backup, config, restore
from test_link_thoughts import NotionMemory

def uid(n):
    return str(uuid.UUID(int=n))


class API(NotionMemory):
    def __init__(self):
        super().__init__()
        self.schemas, self.uploads = {}, {}

    def add(self, parent, children):
        result = super().add(parent, children)
        if parent in self.blocks:
            self.blocks[parent]["has_children"] = bool(self.children.get(parent))
        for block in result:
            media = block[block["type"]]
            if media.get("type") == "file_upload":
                fid = media.pop("file_upload")["id"]
                media.update(type="file", file={"url": "https://files.test/" + fid})
                self.blocks[block["id"]] = copy.deepcopy(block)
        return result

    def call(self, method, path, payload=None):
        if method == "PATCH" and path.startswith("/blocks/") and path.endswith("/children"):
            for b in payload["children"]:
                if "icon" in b[b["type"]] and b[b["type"]]["icon"] is None:
                    raise RuntimeError("Notion rejects null paragraph icons")
                if "list_format" in b[b["type"]]:
                    raise RuntimeError("Notion rejects list_format on write")
                if any(len(r.get("text", {}).get("content", "")) > 2000 for r in b[b["type"]].get("rich_text", [])):
                    raise RuntimeError("Notion rejects text objects over 2000 characters")
                if any(r.get("mention", {}).get("type") == "link_mention" for r in b[b["type"]].get("rich_text", [])):
                    raise RuntimeError("Notion rejects link_mention on write")
        if path == "/file_uploads":
            fid = f"upload-{len(self.uploads)}"
            self.uploads[fid] = None
            return {"id": fid, "upload_url": "https://uploads.test/" + fid}
        if path == "/databases":
            dbid = uid(900 + len(self.schemas))
            schema = {"id": dbid, "parent": payload["parent"], "properties": copy.deepcopy(payload["properties"])}
            for p in schema["properties"].values():
                p["type"] = next(k for k in p if k != "type")
            self.schemas[dbid] = schema
            return copy.deepcopy(schema)
        if path.startswith("/databases/"):
            dbid = path.split("/")[2]
            if method == "PATCH":
                for name, body in payload["properties"].items():
                    body = copy.deepcopy(body)
                    body["type"] = next(iter(body))
                    self.schemas[dbid]["properties"][name] = body
                    if "dual_property" in body.get("relation", {}):
                        rel = body["relation"]
                        self.schemas[rel["database_id"]]["properties"][rel["dual_property"]["synced_property_name"]] = {
                            "type": "relation", "relation": {"database_id": dbid}}
            return copy.deepcopy(self.schemas[dbid])
        result = super().call(method, path, payload)
        if path.startswith("/pages"):
            page = self.pages[result["id"]]
            for p in page["properties"].values():
                p["type"] = next(iter(p))
            page["last_edited_time"] = "2026-09-30T00:00:00Z"
            return copy.deepcopy(page)
        return result

    def pages(self, db):
        return [copy.deepcopy(p) for p in self.pages.values() if p["parent"].get("database_id") == db]

    def children(self, parent):
        b = self.blocks.get(parent, {})
        src = (b.get("synced_block", {}).get("synced_from") or {}).get("block_id")
        return [copy.deepcopy(self.blocks[k]) for k in self.children.get(src or parent, [])]


# The original fake uses pages/children as dict names; adapt callable access
# while keeping its existing block-writing implementation intact.
class CallableDict(dict):
    def __init__(self, owner, fn):
        super().__init__()
        self.owner, self.fn = owner, fn
    def __call__(self, *args):
        return self.fn(self.owner, *args)


class FS:
    def __init__(self):
        self.data = {}
    def document(self, path):
        owner = self
        class Ref:
            def create(self, data):
                assert path not in owner.data
                owner.data[path] = copy.deepcopy(data)
            def get(self):
                class Snap:
                    exists = path in owner.data
                    def to_dict(self):
                        return copy.deepcopy(owner.data[path])
                return Snap()
        return Ref()


class Blob:
    def __init__(self, name, data=b"", content_type="image/png"):
        self.name, self.data, self.content_type = name, data, content_type
        self.generation, self.metadata = 1, None
    def download_as_bytes(self, **kw):
        return self.data
    def exists(self):
        return bool(self.data)
    def upload_from_string(self, data, **kw):
        self.data = data


class GCS:
    def __init__(self):
        self.objects = {}
    def bucket(self, name):
        owner = self
        class Bucket:
            def blob(self, path):
                return owner.objects.setdefault((name, path), Blob(path))
        return Bucket()
    def list_blobs(self, bucket):
        return [b for (name, _), b in self.objects.items() if name == bucket]


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "snapshot.zip"
        self.api, self.fs, self.gcs = API(), FS(), GCS()
        self.api.pages = CallableDict(self.api, API.pages)
        self.api.children = CallableDict(self.api, API.children)
        self.patch = patch.multiple(config, NOTION_MATERIAL_DB=uid(1), NOTION_THOUGHT_DB=uid(2), GCP_PROJECT="test",
                                    NOTION_LIFE_MATERIAL_DB="", NOTION_LIFE_NOTE_DB="", NOTION_WIKI_DB="",
                                    NOTION_WIKI_LOG_DB="")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        for dbid, title, other, relation in ((uid(1), "材料", uid(2), "关联笔记"), (uid(2), "笔记", uid(1), "关联资料")):
            self.api.schemas[dbid] = {"id": dbid, "properties": {
                "标题": {"type": "title", "title": {}}, "主题": {"type": "multi_select", "multi_select": {"options": [{"name": "人工标签"}]}},
                relation: {"type": "relation", "relation": {"database_id": other, "type": "dual_property",
                    "dual_property": {"synced_property_name": "关联资料" if relation == "关联笔记" else "关联笔记"}}}}}
        def page(db, title):
            p = self.api.call("POST", "/pages", {"parent": {"database_id": db}, "properties": {
                "标题": {"title": [{"type": "text", "text": {"content": title}}]}, "主题": {"multi_select": [{"name": "人工标签"}]}}})
            return p["id"]
        self.material, self.note, self.unrelated = page(uid(1), "材料正文"), page(uid(2), "用户想法"), page(uid(1), "另一条")
        self.api.pages[self.material]["properties"]["关联笔记"] = {"type": "relation", "relation": [{"id": self.note}]}
        self.api.pages[self.note]["properties"]["关联资料"] = {"type": "relation", "relation": [{"id": self.material}]}
        body = [{"type": "paragraph", "paragraph": {"icon": None, "rich_text": [{"type": "text", "text": {"content": f"段落 {i}"}}]}} for i in range(120)]
        self.api.add(self.material, [{"type": "heading_2", "heading_2": {"rich_text": [], "is_toggleable": True, "children": body}},
                                     {"type": "image", "image": {"type": "external", "external": {"url": "https://source.test/photo.png"}, "caption": [{"type": "text", "text": {"content": "原图说明"}}]}}])
        src = self.api.add(self.note, [{"type": "synced_block", "synced_block": {"synced_from": None, "children": body[:1]}}])[0]["id"]
        self.source_synced = src
        self.api.add(self.material, [{"type": "synced_block", "synced_block": {"synced_from": {"block_id": src}}}])
        self.gcs.objects[("originals", "original.png")] = Blob("original.png", b"actual-file-bytes")
        typed = {"notion_page_id": self.material, "body_block_ids": [src], "binary": b"raw", "at": datetime(2026, 9, 30, tzinfo=timezone.utc), "n": 2**60}
        wire = backup.Document(fields=backup._helpers.encode_dict(typed))
        self.docs = [{"path": "records/7:1", "wire": base64.b64encode(backup.Document.serialize(wire)).decode()}]
        indexed = backup.Document(fields=backup._helpers.encode_dict({"record_id": "7:1", "url": "https://example.test"}))
        self.docs.append({"path": "urls/index", "wire": base64.b64encode(backup.Document.serialize(indexed)).decode()})
        self.http_get = patch.object(httpx, "get", side_effect=self.get)
        self.http_get.start()
        self.addCleanup(self.http_get.stop)
        self.http_post = patch.object(httpx, "post", side_effect=self.post)
        self.http_post.start()
        self.addCleanup(self.http_post.stop)

    def get(self, url, **kw):
        data = b"actual-file-bytes" if url.startswith("https://source.test") else self.api.uploads[url.rsplit("/", 1)[-1]]
        return httpx.Response(200, content=data, headers={"content-type": "image/png"})

    def post(self, url, **kw):
        self.api.uploads[url.rsplit("/", 1)[-1]] = kw["files"]["file"][1]
        return httpx.Response(200, json={"status": "uploaded"})

    def snapshot(self):
        with patch.object(backup, "export_firestore", return_value=self.docs):
            backup.create_snapshot(self.path, self.api, self.fs, self.gcs, "originals")

    def test_roundtrip_preserves_nested_long_text_bytes_relations_and_typed_mappings(self):
        self.snapshot()
        self.assertEqual(backup.verify_archive(self.path)["counts"]["materials"], 2)
        r = restore.Restorer(self.path, self.api, self.fs, self.gcs, "backup", Path(self.tmp.name) / "report.json")
        result = r.restore(uid(999), [self.material])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["checks"]["pages"], 2)
        self.assertEqual(result["checks"]["shared_note_references"], 1)
        self.assertEqual(result["checks"]["notion_attachment_blocks"], 1)
        image = next(x for x in self.api.children(r.mapped(self.material)) if x["type"] == "image")
        self.assertEqual(image["image"]["caption"][0]["text"]["content"], "原图说明")
        self.assertEqual(result["checks"]["firestore_mappings"], 2)
        self.assertEqual(self.fs.data[f"restore_drills/{result['run_id']}/urls/index"]["record_id"], "7:1")
        self.assertTrue(all(p.startswith("restore_drills/") for p in self.fs.data))
        mapped = next(iter(self.fs.data.values()))
        self.assertEqual(mapped["notion_page_id"], r.mapped(self.material))
        self.assertEqual(mapped["binary"], b"raw")
        self.assertEqual(mapped["n"], 2**60)
        self.assertEqual(mapped["at"], datetime(2026, 9, 30, tzinfo=timezone.utc))
        self.assertNotEqual(mapped["body_block_ids"][0], self.source_synced)
        self.assertEqual(mapped["body_block_ids"][0], r.mapped(self.source_synced))

    def test_tag_versions_operations_and_pending_config_restore_in_isolation(self):
        from app import tag_store
        vocabulary = tag_store.seed()
        vocabulary['pending'] = {'operation': 'job', 'version': 2, 'entries': vocabulary['entries']}
        states = {'settings/tags': vocabulary, 'tag_versions/1': tag_store.seed(),
                  'tag_operations/job': {'status': 'failed', 'pending_version': 2, 'progress': {'options_0': True}},
                  'settings/tag_navigation': {'pages': [{'id': self.material, 'name': 'Agent'}]}}
        for path, state in states.items():
            wire = backup.Document(fields=backup._helpers.encode_dict(state))
            self.docs.append({'path': path, 'wire': base64.b64encode(backup.Document.serialize(wire)).decode()})
        self.snapshot()
        r = restore.Restorer(self.path, self.api, self.fs, self.gcs, 'backup', Path(self.tmp.name) / 'tags.json')
        result = r.restore(uid(999), [self.material])
        root = f"restore_drills/{result['run_id']}/"
        self.assertEqual(self.fs.data[root + 'settings/tags'], vocabulary)
        self.assertEqual(self.fs.data[root + 'tag_versions/1'], states['tag_versions/1'])
        self.assertEqual(self.fs.data[root + 'tag_operations/job'], states['tag_operations/job'])
        self.assertEqual(self.fs.data[root + 'settings/tag_navigation']['pages'][0]['id'], r.mapped(self.material))
        self.assertNotIn('settings/tags', self.fs.data)

    def test_corrupted_archive_fails_before_any_restore_write(self):
        self.snapshot()
        damaged = Path(self.tmp.name) / "damaged.zip"
        with zipfile.ZipFile(self.path) as src, zipfile.ZipFile(damaged, "w") as dst:
            for name in src.namelist():
                dst.writestr(name, b"damaged" if name == "gcs.json" else src.read(name))
        count = len(self.api.pages)
        with self.assertRaisesRegex(ValueError, "checksum"):
            restore.Restorer(damaged, self.api, self.fs, self.gcs, "backup", Path(self.tmp.name) / "report.json")
        self.assertEqual(len(self.api.pages), count)

    def test_failed_file_export_does_not_produce_a_complete_manifest(self):
        with patch.object(backup, "export_firestore", return_value=self.docs), patch.object(Blob, "download_as_bytes", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                backup.create_snapshot(self.path, self.api, self.fs, self.gcs, "originals")
        with zipfile.ZipFile(self.path) as archive:
            self.assertNotIn("manifest.json", archive.namelist())

    def test_production_bucket_and_unsupported_blocks_are_refused(self):
        self.snapshot()
        with self.assertRaisesRegex(ValueError, "separate bucket"):
            restore.Restorer(self.path, self.api, self.fs, self.gcs, "originals", Path(self.tmp.name) / "report.json")
        r = restore.Restorer(self.path, self.api, self.fs, self.gcs, "backup", Path(self.tmp.name) / "report.json")
        r.snapshot["pages"][0]["blocks"].append({"id": "unknown", "type": "unsupported"})
        with self.assertRaisesRegex(ValueError, "Unsupported block"):
            r.restore(uid(999), [self.material])
        self.assertEqual(len(self.api.pages), 3)

    def test_restore_unique_prefix_is_valid_independent_and_keeps_original(self):
        original = {"properties": {"记录编号": {"type": "unique_id", "unique_id": {"prefix": "SRC"}}}}
        a, b = restore.schema_properties(original), restore.schema_properties(original)
        self.assertRegex(a["记录编号"]["unique_id"]["prefix"], r"^[A-Za-z][A-Za-z0-9-]{1,9}$")
        self.assertNotEqual(a, b)
        self.assertEqual(original["properties"]["记录编号"]["unique_id"]["prefix"], "SRC")

    def test_schedule_requires_a_live_attachment_and_edit_drill(self):
        import schedule_backup
        path = Path(self.tmp.name) / "schedule-report.json"
        path.write_text(json.dumps({"status": "verified", "checks": {"pages": 2}}))
        with self.assertRaises(ValueError):
            schedule_backup.validated_report(path)
        path.write_text(json.dumps({"status": "verified", "checks": {"pages": 2, "firestore_mappings": 2,
            "gcs_objects": 1, "shared_note_references": 1, "shared_edit_verified": True, "notion_attachment_blocks": 1}}))
        self.assertEqual(schedule_backup.validated_report(path)["status"], "verified")

    def test_schedule_gate_follows_mirrors_and_covers_every_role(self):
        import schedule_backup
        path = Path(self.tmp.name) / "schedule-report.json"
        base = {"pages": 3, "firestore_mappings": 1, "gcs_objects": 1, "notion_attachment_blocks": 1,
                "synced_mirrors_in_group": 0}
        path.write_text(json.dumps({"status": "verified", "checks": {**base, "pages_by_role": {"materials": 2}}}))
        with self.assertRaisesRegex(ValueError, "thoughts"):
            schedule_backup.validated_report(path)
        path.write_text(json.dumps({"status": "verified", "checks": {**base, "pages_by_role": {"materials": 2, "thoughts": 1}}}))
        self.assertEqual(schedule_backup.validated_report(path)["status"], "verified")
        path.write_text(json.dumps({"status": "verified", "checks": {**base, "synced_mirrors_in_group": 1,
                                                                     "pages_by_role": {"materials": 2, "thoughts": 1}}}))
        with self.assertRaises(ValueError):
            schedule_backup.validated_report(path)

    def wiki_fixture(self):
        wiki = uid(3)
        self.api.schemas[wiki] = {"id": wiki, "properties": {
            "标题": {"type": "title", "title": {}},
            "内容状态": {"type": "select", "select": {"options": [{"name": "草稿"}]}},
            "相关 Wiki": {"type": "relation", "relation": {"database_id": wiki, "type": "single_property", "single_property": {}}},
            "科研笔记": {"type": "relation", "relation": {"database_id": uid(2), "type": "single_property", "single_property": {}}}}}
        def entry(title, props):
            return self.api.call("POST", "/pages", {"parent": {"database_id": wiki}, "properties": {
                "标题": {"title": [{"type": "text", "text": {"content": title}}]},
                "内容状态": {"select": {"name": "草稿"}}, **props}})["id"]
        a = entry("条目 A", {"科研笔记": {"relation": [{"id": self.note}]}, "相关 Wiki": {"relation": []}})
        b = entry("条目 B", {"科研笔记": {"relation": []}, "相关 Wiki": {"relation": [{"id": a}]}})
        cell = lambda t: [{"type": "text", "text": {"content": t}}]
        rows = [{"type": "table_row", "table_row": {"cells": [cell(f"行 {i}"), cell(str(i))]}} for i in range(130)]
        self.api.add(b, [
            {"type": "heading_4", "heading_4": {"rich_text": cell("四级标题"), "is_toggleable": False}},
            {"type": "to_do", "to_do": {"rich_text": cell("待核实"), "checked": True}},
            {"type": "numbered_list_item", "numbered_list_item": {"rich_text": cell("第一项"), "list_format": "numbers"}},
            {"type": "code", "code": {"language": "markdown", "caption": [], "rich_text": cell("长" * 4500 + "😀" * 10)}},
            {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [*cell("见 "), {
                "type": "mention", "mention": {"type": "link_mention", "link_mention": {"href": "https://arxiv.org/abs/1"}},
                "annotations": {"bold": False}, "plain_text": "arXiv 1", "href": "https://arxiv.org/abs/1"}]}},
            {"type": "table", "table": {"table_width": 2, "has_column_header": True, "has_row_header": False,
                                        "children": rows}}])
        return wiki, a, b

    def test_wiki_role_tables_todos_and_one_way_self_relations_roundtrip(self):
        wiki, a, b = self.wiki_fixture()
        with patch.object(config, "NOTION_WIKI_DB", wiki):
            self.snapshot()
            self.assertEqual(backup.verify_archive(self.path)["counts"]["wiki"], 2)
            r = restore.Restorer(self.path, self.api, self.fs, self.gcs, "backup", Path(self.tmp.name) / "wiki.json")
            result = r.restore(uid(999), [b])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["checks"]["tables"], 1)
        self.assertEqual(result["checks"]["link_mentions_as_links"], 1)
        item = next(x for x in self.api.children(r.mapped(b)) if x["type"] == "bulleted_list_item")
        self.assertEqual(item["bulleted_list_item"]["rich_text"][1]["text"],
                         {"content": "arXiv 1", "link": {"url": "https://arxiv.org/abs/1"}})
        self.assertEqual(result["checks"]["synced_mirrors_in_group"], 1)
        self.assertEqual(set(result["checks"]["pages_by_role"]), {"wiki", "thoughts", "materials"})
        new_a, new_b = r.mapped(a), r.mapped(b)
        self.assertNotIn(new_b, (a, b))
        self.assertEqual(self.api.pages[new_b]["properties"]["相关 Wiki"]["relation"], [{"id": new_a}])
        self.assertEqual(self.api.pages[new_a]["properties"]["相关 Wiki"]["relation"], [])  # no reverse link
        self.assertEqual(self.api.pages[new_a]["properties"]["科研笔记"]["relation"], [{"id": r.mapped(self.note)}])
        table = next(x for x in self.api.children(new_b) if x["type"] == "table")
        self.assertEqual(len(self.api.children(table["id"])), 130)
        self.assertEqual(self.api.schemas[result["databases"]["wiki"]]["properties"]["相关 Wiki"]["relation"]["database_id"],
                         result["databases"]["wiki"])

    def test_relation_to_unshared_database_refuses_snapshot(self):
        self.api.pages[self.unrelated]["properties"]["生活笔记"] = {"type": "relation", "relation": []}
        with patch.object(backup, "export_firestore", return_value=self.docs):
            with self.assertRaisesRegex(RuntimeError, "not shared"):
                backup.create_snapshot(self.path, self.api, self.fs, self.gcs, "originals")
        with zipfile.ZipFile(self.path) as archive:
            self.assertNotIn("manifest.json", archive.namelist())

    def test_resume_reuses_test_pages_after_a_rejected_body_write(self):
        self.snapshot()
        report = Path(self.tmp.name) / "resume.json"
        r = restore.Restorer(self.path, self.api, self.fs, self.gcs, "backup", report)
        call = self.api.call
        failed = False
        def interrupt(method, path, payload=None):
            nonlocal failed
            if (not failed and method == "PATCH" and path.endswith("/children")
                    and self.api.blocks.get(path.split('/')[2], {}).get('type') == 'synced_block'):
                failed = True
                raise RuntimeError("Body write rejected before saving")
            return call(method, path, payload)
        with patch.object(self.api, 'call', side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, "Body write"):
                r.restore(uid(999), [self.material])
        created = len(self.api.pages)
        resumed = restore.Restorer(self.path, self.api, self.fs, self.gcs, "backup", report, resume=True)
        result = resumed.restore(uid(999), [self.material])
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(len(self.api.pages), created)

    def test_changed_source_does_not_publish_a_completed_snapshot(self):
        with patch.object(backup, 'export_firestore', side_effect=[self.docs, self.docs + [{'path': 'records/new'}]]):
            with self.assertRaisesRegex(RuntimeError, 'Firestore changed'):
                backup.create_snapshot(self.path, self.api, self.fs, self.gcs, 'originals')
        with zipfile.ZipFile(self.path) as archive:
            self.assertNotIn('manifest.json', archive.namelist())

    def test_read_transport_errors_retry_but_unknown_writes_do_not(self):
        api = backup.NotionAPI()
        with patch.object(backup.time, 'sleep'), patch.object(httpx, 'request', side_effect=[
                httpx.ReadTimeout('offline'), httpx.Response(200, json={'ok': True})]) as request:
            self.assertTrue(api.call('GET', '/pages/test')['ok'])
            self.assertEqual(request.call_count, 2)
        with patch.object(backup.time, 'sleep'), patch.object(httpx, 'request', side_effect=httpx.ReadTimeout('unknown outcome')) as request:
            with self.assertRaises(httpx.ReadTimeout):
                api.call('POST', '/pages', {})
            self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
