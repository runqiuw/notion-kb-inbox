"""Opt-in live video drill: real B站/YouTube/Gemini/LLM, NEW Notion databases, isolated Firestore.

Runs the production code path (pipeline._handle_url -> video jobs -> Notion) from
a temporary Cloud Run Job, because cloud egress is what production sees (B站's
view API 412s from a home connection but not from Cloud Run). Nothing touches
production pages: materials and notes go to new databases under a temporary root
page, Firestore state lives under restore_drills/<run>, archives are prefixed
drill-<run>-, Telegram is stubbed, and the queue is simulated in-process with
the same resume() the /tasks/videos worker calls. The production databases are
compared before and after. The root page is moved to trash on success.

--gemini-model overrides the transcription model for this drill only (the
production model's free daily quota may already be spent).
"""
import argparse
import copy
import json
import os
import sys
import time
import traceback
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, notion, pipeline, settings, storage, store, tasks, tag_notion, telegram, video
from verify_tag_runtime import Namespace, stable

COPYABLE = {"title", "rich_text", "select", "multi_select", "url", "files", "number", "date",
            "checkbox", "created_time", "last_edited_time", "status"}


def clone_schema(source_id, parent, title, relation_targets=None):
    schema = notion._call("GET", "/databases/" + source_id)
    props = {}
    for name, prop in schema["properties"].items():
        kind = prop["type"]
        if kind in ("select", "multi_select"):
            props[name] = {kind: {"options": [{"name": o["name"]} for o in prop[kind]["options"]]}}
        elif kind == "status":
            continue   # status options cannot be created through the API
        elif kind in COPYABLE:
            props[name] = {kind: {}}
        elif kind == "relation" and relation_targets and prop["relation"]["database_id"].replace("-", "") in relation_targets:
            target = relation_targets[prop["relation"]["database_id"].replace("-", "")]
            props[name] = {"relation": {"database_id": target, "single_property": {}}}
    return notion._call("POST", "/databases", {"parent": {"page_id": parent}, "title": notion._rt(title),
                                               "properties": props})["id"]


def page_text(block_id):
    out = []
    for b in notion.list_children(block_id):
        body = b.get(b["type"], {})
        out.append("".join(t.get("plain_text", "") for t in body.get("rich_text", [])))
        if b.get("has_children"):
            out.append(page_text(b["id"]))
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gemini-model", default="")
    parser.add_argument("--keep", action="store_true", help="keep the temporary Notion root")
    args = parser.parse_args()
    run = "video-drill-" + uuid.uuid4().hex[:10]
    report = {"run": run, "started": time.time(), "gemini_model": args.gemini_model or config.GEMINI_MODEL,
              "samples": [], "checks": []}
    provider = settings.active()            # production provider, read before isolating Firestore
    report["llm_model"] = provider["model"]
    production = [config.NOTION_MATERIAL_DB, config.NOTION_THOUGHT_DB]
    before = {d: stable(notion.query_database(d)) for d in production}
    schema = notion._call("GET", "/databases/" + production[0])
    parent = tag_notion.containing_page(schema["parent"])
    root = notion._call("POST", "/pages", {"parent": {"page_id": parent},
                                           "properties": {"title": notion._title("视频入库隔离验证 " + run)}})
    report["root_id"] = root["id"]
    sent, scheduled = [], []
    real_archive = storage.archive
    ok = False
    try:
        mat = clone_schema(production[0], root["id"], "视频测试资料")
        note = clone_schema(production[1], root["id"], "视频测试笔记", {production[0].replace("-", ""): mat})
        patches = [
            patch.object(config, "NOTION_MATERIAL_DB", mat), patch.object(config, "NOTION_THOUGHT_DB", note),
            patch.object(config, "SERVICE_URL", "https://drill.invalid"), patch.object(config, "TASKS_SA_EMAIL", "drill@invalid"),
            patch.object(store, "_db", Namespace(store.db(), run)),
            patch.object(settings, "active", lambda: provider),
            patch.object(telegram, "send", lambda *a, **k: sent.append(("send", a[1] if len(a) > 1 else "")) or {"message_id": 1}),
            patch.object(telegram, "edit", lambda *a, **k: sent.append(("edit", a[2] if len(a) > 2 else ""))),
            patch.object(telegram, "delete", lambda *a, **k: None),
            patch.object(tasks, "enqueue_video", lambda key, n, delay=1: scheduled.append((key, delay)) or True),
            patch.object(storage, "archive", lambda data, name, ct="application/octet-stream": real_archive(data, f"drill-{run}-{name}", ct)),
        ]
        if args.gemini_model:
            patches.append(patch.object(config, "GEMINI_MODEL", args.gemini_model))
        for p in patches:
            p.start()
        mid = [100]

        def send(url, text=""):
            mid[0] += 1
            msg = {"chat": {"id": -1}, "message_id": mid[0], "text": f"{url} {text}".strip()}
            started = time.monotonic()
            reply, link = pipeline._handle_url(msg, url, text, lambda s: None, update_id=None)
            return {"url": url, "reply": reply, "link": link, "seconds": round(time.monotonic() - started, 1)}

        def drive(key, rounds=12):
            for _ in range(rounds):
                job = store.get_video_job(key) or {}
                if job.get("status") in ("done", "staged"):
                    break
                store.update_video_job(key, next_retry_at=0)   # the scheduled time has come
                video.resume(key)
            return store.get_video_job(key) or {}

        def check(name, cond, detail=""):
            report["checks"].append({"check": name, "ok": bool(cond), "detail": str(detail)[:300]})
            print(("PASS " if cond else "FAIL ") + name + (f" :: {detail}" if not cond else ""), flush=True)

        samples = [
            ("bili_long", "https://www.bilibili.com/video/BV1ZYJC6MEH2", "这期讲课程地图，回头对照我们自己的教研计划", "bilibili:BV1ZYJC6MEH2:"),
            ("bili_part", "https://www.bilibili.com/video/BV1DTxXzUEvW?p=2&t=60", "", "bilibili:BV1DTxXzUEvW:"),
            ("bili_none", "https://www.bilibili.com/video/BV13x41117TL", "这首歌的现场版", "bilibili:BV13x41117TL:"),
            ("yt_captioned", "https://youtu.be/aircAruvnKk", "", "youtube:aircAruvnKk"),
            ("yt_uncaptioned", "https://www.youtube.com/watch?v=3Yj_R8Ejqx8", "", "youtube:3Yj_R8Ejqx8"),
        ]
        for name, url, text, prefix in samples:
            print("sample", name, flush=True)
            first = send(url, text)
            jobs = [d.to_dict() for d in store.db().collection("video_jobs").stream()]
            key = next(j["key"] for j in jobs if j["key"].startswith(prefix))
            job = drive(key)
            row = {"sample": name, "first": first, "key": key, "status": job.get("status"), "error": job.get("error"),
                   "alert": job.get("alert"), "source_kind": job.get("source_kind"), "chars": job.get("transcript_chars"),
                   "sections": len(job.get("section_steps") or {}), "chunks": len(job.get("chunks") or {}),
                   "wait_count": job.get("wait_count"), "page": (job.get("page") or {}).get("url")}
            if job.get("page"):
                body = page_text(job["page"]["id"])
                transcript = video.get_json(job["transcript_uri"])
                last = transcript["segments"][-1]["text"]
                p = notion._call("GET", "/pages/" + job["page"]["id"])
                row.update(processing=notion.prop_text(p, "处理状态"), title=notion.prop_text(p, "标题"),
                           last_line_on_page=last[:40] in body, overview=job.get("summary_uri") is not None,
                           headings=[h for h in ("中文概览", "章节要点", "完整转录", "转录来源与范围", "我的想法") if h in body])
            report["samples"].append(row)
            print(json.dumps(row, ensure_ascii=False)[:600], flush=True)

        by = {r["sample"]: r for r in report["samples"]}
        for name in ("bili_long", "bili_part", "yt_captioned", "yt_uncaptioned"):
            r = by[name]
            check(f"{name} published complete", r["status"] == "done" and r.get("processing") == "完成", r)
            check(f"{name} full transcript on page", r.get("last_line_on_page"), r.get("key"))
        check("bili_long exceeds 40000 chars and every section summarized",
              (by["bili_long"]["chars"] or 0) > 40000 and by["bili_long"]["sections"] >= 4, by["bili_long"])
        check("bili_none waited then staged (not an alert)",
              by["bili_none"]["status"] == "staged" and by["bili_none"]["wait_count"] == len(video.BILI_WAIT_DELAYS)
              and not by["bili_none"]["alert"] and "暂无字幕" in (by["bili_none"]["error"] or ""), by["bili_none"])
        check("bili_none staged entry", store.get_staged("https://www.bilibili.com/video/BV13x41117TL/"))
        check("youtube is model_transcript", by["yt_captioned"]["source_kind"] == "model_transcript")
        # Duplicates by other spellings, one carrying a thought.
        dup1 = send("https://m.bilibili.com/video/BV1DTxXzUEvW?p=2", "")
        dup2 = send("https://www.youtube.com/watch?v=aircAruvnKk&t=30s", "第二遍看，梯度那段终于明白了")
        check("duplicate B站 spelling not re-imported", "已保存过" in dup1["reply"], dup1)
        check("duplicate YouTube spelling not re-imported", "已保存过" in dup2["reply"] or "之前存过" in dup2["reply"], dup2)
        materials = notion.query_database(mat)
        thoughts = notion.query_database(note)
        report["materials"] = len(materials)
        report["thoughts"] = [notion.prop_text(t, "标题") for t in thoughts]
        check("one page per video entity", len(materials) == 4, len(materials))
        check("attached thoughts kept (3 notes)", len(thoughts) == 3, report["thoughts"])
        report["scheduled_delays"] = [d for _, d in scheduled]
        report["telegram"] = sent[-12:]
        for d in production:
            check("production database unchanged " + d[:8], stable(notion.query_database(d)) == before[d])
        ok = all(c["ok"] for c in report["checks"])
        report["status"] = "passed" if ok else "failed"
    except Exception as e:
        report.update(status="error", error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-3000:])
        raise
    finally:
        patch.stopall()
        report["seconds"] = round(time.time() - report["started"])
        if ok and not args.keep:
            notion._call("PATCH", "/pages/" + root["id"], {"archived": True})
            report["root_archived"] = True
        print("REPORT " + json.dumps(report, ensure_ascii=False), flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
