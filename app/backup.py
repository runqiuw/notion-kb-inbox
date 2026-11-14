"""Portable, byte-complete KB snapshots. No ingestion, LLM or production writes.

Run `python -m app.backup create --help`. A snapshot is a ZIP containing raw
Notion schemas/pages/block trees, typed Firestore documents, and actual file
bytes. Only fully collected and checksum-verified snapshots are published.
"""
import argparse
import base64
import hashlib
import json
import mimetypes
import os
import subprocess
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from google.cloud import firestore, storage
from google.cloud.firestore_v1 import _helpers
from google.cloud.firestore_v1.types import Document
from google.oauth2.credentials import Credentials

from . import config

FORMAT = 1


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def key(value):
    return value.replace("-", "").lower()


def now():
    return datetime.now(timezone.utc).isoformat()


class NotionAPI:
    def __init__(self):
        self.last = 0

    def call(self, method, path, payload=None):
        for attempt in range(5):
            time.sleep(max(0, .36 - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            try:
                r = httpx.request(method, "https://api.notion.com/v1" + path,
                                 headers={"Authorization": "Bearer " + config.NOTION_TOKEN,
                                          "Notion-Version": config.NOTION_VERSION},
                                 json=payload, timeout=httpx.Timeout(40, connect=15))
            except httpx.TransportError:
                if (method == "GET" or path.endswith("/query")) and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise  # Unknown write outcome must not be blindly repeated.
            if r.status_code >= 500 and (method == "GET" or path.endswith("/query")) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 and attempt < 4:
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if r.status_code >= 400:
                # Signed URLs and credentials must never appear in job logs.
                raise RuntimeError(f"Notion {method} {path}: HTTP {r.status_code}, "
                                   f"code={r.json().get('code', 'unknown')}")
            return r.json()

    def pages(self, dbid):
        out, cursor = [], None
        while True:
            payload = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            res = self.call("POST", f"/databases/{dbid}/query", payload)
            out.extend(res["results"])
            if not res.get("has_more"):
                return out
            cursor = res["next_cursor"]

    def children(self, bid):
        out, cursor = [], None
        while True:
            path = f"/blocks/{bid}/children?page_size=100"
            if cursor:
                path += "&start_cursor=" + cursor
            res = self.call("GET", path)
            out.extend(res["results"])
            if not res.get("has_more"):
                return out
            cursor = res["next_cursor"]


class ArchiveWriter:
    def __init__(self, path):
        self.stream = open(path, "x+b")
        os.fchmod(self.stream.fileno(), 0o600)
        self.zip = zipfile.ZipFile(self.stream, "w", zipfile.ZIP_DEFLATED)
        self.entries = {}

    def write(self, name, data):
        if name not in self.entries:
            self.zip.writestr(name, data)
            self.entries[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    def asset(self, data, filename, content_type):
        digest = hashlib.sha256(data).hexdigest()
        path = "bytes/" + digest
        self.write(path, data)
        filename = filename or "attachment"
        if not Path(filename).suffix:
            filename += mimetypes.guess_extension(content_type.split(";", 1)[0]) or ".bin"
        return {"entry": path, "sha256": digest, "filename": filename, "content_type": content_type}


def capture_files(value, writer, cache):
    """Keep the original API response and attach a portable byte descriptor."""
    if isinstance(value, dict):
        if value.get("type") in ("file", "external"):
            url = (value.get(value["type"]) or {}).get("url")
            if url:
                if url not in cache:
                    r = httpx.get(url, follow_redirects=True, timeout=120)
                    if r.status_code != 200:
                        raise RuntimeError(f"Attachment download: HTTP {r.status_code}")
                    cache[url] = writer.asset(r.content, value.get("name") or
                                              unquote(urlsplit(url).path.rsplit("/", 1)[-1]),
                                              r.headers.get("content-type", "application/octet-stream"))
                value["_backup_asset"] = cache[url]
        for name, child in list(value.items()):
            if name != "_backup_asset":
                capture_files(child, writer, cache)
    elif isinstance(value, list):
        for child in value:
            capture_files(child, writer, cache)


def tree(api, parent, writer, cache):
    out = api.children(parent)
    for block in out:
        if block["type"] in ("unsupported", "child_page", "child_database"):
            raise ValueError("Cannot make a complete backup of block type: " + block["type"])
        capture_files(block, writer, cache)
        # A mirror's children belong to its source, exported once, not twice.
        source = (block.get("synced_block", {}).get("synced_from") or {}).get("block_id")
        if block.get("has_children") and not source:
            block["_children"] = tree(api, block["id"], writer, cache)
    return out


def export_firestore(client):
    docs = []
    def collect(collection):
        # list_documents also discovers documents which only have subcollections.
        for ref in collection.list_documents():
            snap = ref.get()
            if snap.exists:
                wire = Document(fields=_helpers.encode_dict(snap.to_dict()))
                docs.append({"path": ref.path,
                             "wire": base64.b64encode(Document.pb(wire).SerializeToString(deterministic=True)).decode()})
            for sub in ref.collections():
                collect(sub)
    for collection in client.collections():
        if collection.id != "restore_drills":
            collect(collection)
    return sorted(docs, key=lambda d: d["path"])


def databases():
    """Every database in scope, by role. The first two are the bot's own."""
    roles = (("materials", config.NOTION_MATERIAL_DB), ("thoughts", config.NOTION_THOUGHT_DB),
             ("life_materials", config.NOTION_LIFE_MATERIAL_DB), ("life_notes", config.NOTION_LIFE_NOTE_DB),
             ("wiki", config.NOTION_WIKI_DB), ("wiki_log", config.NOTION_WIKI_LOG_DB))
    return [(kind, dbid) for kind, dbid in roles if dbid]


def hidden_relations(schema, page):
    """A relation to a database not shared with the integration is dropped from
    the schema while pages still return it empty: the values would be lost
    without any API error."""
    return [name for name, prop in page["properties"].items()
            if prop.get("type") == "relation" and name not in schema["properties"]]


def create_snapshot(path, api, fs, gcs, source_bucket):
    started = now()
    writer = ArchiveWriter(path)
    try:
        cache, dbs, pages = {}, {}, []
        for kind, dbid in databases():
            schema = api.call("GET", "/databases/" + dbid)
            capture_files(schema, writer, cache)
            dbs[kind] = schema
            for page in api.pages(dbid):
                if hidden_relations(schema, page):
                    raise RuntimeError(f"{kind}: relation target not shared with the integration; "
                                       "connect it in Notion before backing up")
                for prop in page["properties"].values():
                    if prop.get("type") == "relation" and prop.get("has_more"):
                        relation, cursor = [], None
                        while True:
                            endpoint = f"/pages/{page['id']}/properties/{prop['id']}?page_size=100"
                            if cursor:
                                endpoint += "&start_cursor=" + cursor
                            res = api.call("GET", endpoint)
                            relation.extend(p["relation"] for p in res["results"])
                            if not res.get("has_more"):
                                break
                            cursor = res["next_cursor"]
                        prop["relation"], prop["has_more"] = relation, False
                capture_files(page, writer, cache)
                blocks = tree(api, page["id"], writer, cache)
                check = api.call("GET", "/pages/" + page["id"])
                if check["last_edited_time"] != page["last_edited_time"]:
                    raise RuntimeError("A Notion page changed during capture; retry the snapshot")
                pages.append({"kind": kind, "page": page, "blocks": blocks})
        writer.write("notion.json", canonical({"databases": dbs, "pages": pages}))
        documents = export_firestore(fs)
        writer.write("firestore.json", canonical(documents))
        objects = []
        for blob in gcs.list_blobs(source_bucket):
            data = blob.download_as_bytes(if_generation_match=blob.generation)
            asset = writer.asset(data, blob.name.rsplit("/", 1)[-1],
                                 blob.content_type or "application/octet-stream")
            objects.append({"name": blob.name, "generation": str(blob.generation),
                            "content_type": blob.content_type, "metadata": blob.metadata,
                            "asset": asset})
        writer.write("gcs.json", canonical(objects))
        # There is no cross-service transaction. Reject a moving dataset rather
        # than publish a mixed snapshot as complete; the job can retry later.
        for kind, schema in dbs.items():
            current = {p["id"]: p["last_edited_time"] for p in api.pages(schema["id"])}
            expected = {p["page"]["id"]: p["page"]["last_edited_time"] for p in pages if p["kind"] == kind}
            if current != expected:
                raise RuntimeError("Notion changed during backup; retry when writes settle")
        current_objects = {b.name: str(b.generation) for b in gcs.list_blobs(source_bucket)}
        if current_objects != {o["name"]: o["generation"] for o in objects}:
            raise RuntimeError("GCS changed during backup; retry when writes settle")
        if export_firestore(fs) != documents:
            raise RuntimeError("Firestore changed during backup; retry when writes settle")
        manifest = {"format": FORMAT, "started_at": started, "completed_at": now(),
                    "source": {"project": config.GCP_PROJECT, "bucket": source_bucket,
                               "databases": {k: v["id"] for k, v in dbs.items()}},
                    "counts": {**{kind: sum(p["kind"] == kind for p in pages) for kind in dbs},
                               "firestore_documents": len(documents), "gcs_objects": len(objects)},
                    "entries": writer.entries}
        writer.zip.writestr("manifest.json", canonical(manifest))
        return manifest
    finally:
        writer.zip.close()
        writer.stream.close()


def verify_archive(path):
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != FORMAT or not manifest.get("completed_at"):
            raise ValueError("Incomplete or unsupported backup")
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != {*manifest["entries"], "manifest.json"}:
            raise ValueError("Backup entry inventory mismatch")
        for name, expected in manifest["entries"].items():
            data = archive.read(name)
            if len(data) != expected["size"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
                raise ValueError("Backup checksum mismatch: " + name)
        return manifest


def clients(local_gcloud=False, project=None):
    if local_gcloud:
        project = project or config.GCP_PROJECT
        def gc(*args):
            return subprocess.run(["gcloud", *args, "--project=" + project],
                                  check=True, text=True, capture_output=True).stdout
        service = json.loads(gc("run", "services", "describe", "kb-bot", "--region=us-central1", "--format=json"))
        env = service["spec"]["template"]["spec"]["containers"][0].get("env", [])
        for e in env:
            if e["name"] in ("GCP_PROJECT", "GCS_BUCKET", "NOTION_MATERIAL_DB", "NOTION_THOUGHT_DB", "NOTION_VERSION") and "value" in e:
                setattr(config, e["name"], e["value"])
        config.NOTION_TOKEN = gc("secrets", "versions", "access", "latest", "--secret=kb-notion-token").strip()
        # A fixed access token expires mid-drill (gcloud may hand out one that is
        # already near expiry). Ask gcloud again whenever google-auth refreshes.
        def refresh(request, scopes):
            token = gc("auth", "print-access-token").strip()
            return token, datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5)
        credentials = Credentials(token=None, refresh_handler=refresh)
    else:
        credentials = None
    return (NotionAPI(), firestore.Client(project=config.GCP_PROJECT, credentials=credentials),
            storage.Client(project=config.GCP_PROJECT, credentials=credentials))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-gcloud", action="store_true", help="Read existing service config; credentials stay in memory")
    parser.add_argument("--project", default=config.GCP_PROJECT)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--output", default="/tmp/kb-backup.zip")
    create.add_argument("--bucket", default=os.environ.get("BACKUP_BUCKET"))
    verify = sub.add_parser("verify")
    verify.add_argument("archive")
    args = parser.parse_args()
    if args.command == "verify":
        print(json.dumps(verify_archive(args.archive)["counts"], ensure_ascii=False))
        return
    api, fs, gcs = clients(args.local_gcloud, args.project)
    path = Path(args.output)
    if path.exists():
        raise ValueError("Refusing to overwrite a backup")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(path) + ".partial")
    try:
        create_snapshot(partial, api, fs, gcs, config.GCS_BUCKET)
        manifest = verify_archive(partial)
        partial.replace(path)
        result = {"archive": str(path), "counts": manifest["counts"]}
        if args.bucket:
            if args.bucket == config.GCS_BUCKET:
                raise ValueError("Backups must use a separate bucket")
            object_name = "snapshots/" + datetime.now(timezone.utc).strftime("%Y/%m/%d/%H%M%S-") + uuid.uuid4().hex[:8] + ".zip"
            blob = gcs.bucket(args.bucket).blob(object_name)
            blob.upload_from_filename(str(path), content_type="application/zip", if_generation_match=0)
            result["uri"] = f"gs://{args.bucket}/{object_name}"
        print(json.dumps(result, ensure_ascii=False))
    finally:
        partial.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
