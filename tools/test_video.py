"""Video ingestion: entity dedupe, B站 subtitles/waiting/alerts, YouTube slices and quota, full summary."""
import copy
import datetime
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from app import config, enrich, notion, pipeline, settings, storage, store, tag_store, tasks, telegram, video, work_budget
from test_park_expiry import Database
from test_image_batches import Memory

BV = "BV1DTxXzUEvW"
LONG_LINE = "这是第{}句完整字幕，讲到模型偏差与损失函数的具体例子。"
REAL_QUOTA_DAY = video.quota_day


def subtitle_body(n):
    return [{"from": i * 2.0, "to": i * 2.0 + 1.8, "content": LONG_LINE.format(i)} for i in range(n)]


class FakeClient:
    def __init__(self, route):
        self.route = route

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        return self.route(httpx.Request("GET", url, params=params) if params is not None else httpx.Request("GET", url))


class FilteredMemory(Memory):
    """Honours the 原始链接 equality filter, as the Notion API does."""
    def call(self, method, path, payload=None):
        result = super().call(method, path, payload)
        url = ((payload or {}).get("filter") or {}).get("url", {}).get("equals")
        if path.startswith("/databases/") and url:
            result = {"results": [p for p in result["results"] if notion.prop_text(p, "原始链接") == url]}
        return result


def ok(req, value):
    return httpx.Response(200, json=value, request=req)


class Videos(unittest.TestCase):
    def setUp(self):
        self.db = Database(); self.n = FilteredMemory(); self.files = {}; self.scheduled = []; self.sent = []
        self.p(store, "_db", self.db); self.p(store.firestore, "transactional", lambda f: f)
        for name, value in (("GCP_PROJECT", "test"), ("GCS_BUCKET", "test"), ("NOTION_MATERIAL_DB", "materials"),
                            ("NOTION_THOUGHT_DB", "thoughts"), ("GEMINI_API_KEY", "k"), ("SERVICE_URL", ""),
                            ("TASKS_SA_EMAIL", "")):
            self.p(config, name, value)
        self.p(notion, "_call", self.n.call); self.p(notion, "_create_tagged_page", lambda payload: self.n.call("POST", "/pages", payload))
        self.p(notion, "related_topics", lambda ids: [])
        self.p(pipeline.thumbnails, "material_art", lambda *a, **k: None)
        self.p(telegram, "note_deeplink", lambda pid: "https://t.me/test?start=" + pid)
        self.p(telegram, "edit", lambda *a, **k: None)
        self.p(telegram, "send", lambda *a, **k: self.sent.append((a, k)))
        self.p(settings, "active", lambda: {"base_url": "https://test/v1", "model": "test", "api_key": ""})
        self.p(tag_store, "load", lambda *a, **k: {"version": 1, "entries": []})
        self.p(tag_store, "normalize", lambda names, *a, **k: [])
        self.p(storage, "archive", self.archive); self.p(storage, "read_archive", lambda uri, **k: self.files[uri])
        self.p(enrich, "_chat", self.model)
        self.p(tasks, "enqueue_video", lambda key, n, delay=1: self.scheduled.append((key, delay)) or True)
        self.p(video.time, "sleep", lambda s: None)
        self.p(video.httpx, "Client", lambda **kw: FakeClient(self.bili))
        self.p(video.httpx, "get", lambda url, params=None, **kw: self.youtube(httpx.Request("GET", url, params=params)))
        self.p(video.httpx, "post", lambda url, json=None, **kw: self.gemini(json))
        self.sections_seen, self.overall_inputs, self.gemini_calls = [], [], []
        self.dm = {"subtitles": [{"lan": "ai-zh", "lan_doc": "中文（自动生成）", "type": 1, "ai_type": 0,
                                  "subtitle_url": "//aisubtitle.hdslb.com/bfs/sub.json?auth_key=signed"},
                                 {"lan": "ai-en", "lan_doc": "英语（自动翻译）", "type": 1, "ai_type": 1,
                                  "subtitle_url": "//aisubtitle.hdslb.com/bfs/en.json?auth_key=signed"}]}
        self.dm_code, self.need_login, self.body = 0, True, subtitle_body(3000)
        self.gemini_plan = []
        self.day = ["2026-10-16"]   # the Gemini quota day; tests move it on to simulate a reset
        self.p(video, "quota_day", lambda now: self.day[0])
        self.view_blocked = False
        self.msg = {"chat": {"id": 7}, "message_id": 1}

    def p(self, obj, name, value):
        q = patch.object(obj, name, value); q.start(); self.addCleanup(q.stop); return value

    def archive(self, data, name, mime="text/plain"):
        uri = "gs://test/" + name; self.files[uri] = data; return uri

    # ----- fakes -----
    def bili(self, req):
        url = str(req.url)
        if url.startswith("https://b23.tv/"):
            return httpx.Response(200, text="", request=httpx.Request("GET", f"https://www.bilibili.com/video/{BV}/?p=2&share_source=copy"))
        if url == "https://www.bilibili.com/":
            return httpx.Response(200, text="<html>", request=req)
        if "/x/player/pagelist" in url:
            return ok(req, {"code": 0, "data": [{"cid": 111, "part": "上", "duration": 6000},
                                                {"cid": 222, "part": "下", "duration": 6000}]})
        if "/x/web-interface/view" in url:
            return httpx.Response(412, text="blocked", request=req)
        if "/x/web-interface/wbi/view" in url:
            if self.view_blocked:
                return httpx.Response(412, text="blocked", request=req)
            return ok(req, {"code": 0, "data": {"title": "李宏毅机器学习", "pubdate": 1760000000, "pic": "http://i0.hdslb.com/c.jpg",
                                               "owner": {"name": "李宏毅-机器学习"}}})
        if "/x/v2/dm/view" in url:
            return ok(req, {"code": self.dm_code, "data": {"subtitle": {"lan": "", **self.dm}}})
        if "/x/player/v2" in url:
            return ok(req, {"code": 0, "data": {"need_login_subtitle": self.need_login}})
        if "aisubtitle.hdslb.com" in url:
            # The signed query string must survive: no params= on this request.
            if req.url.params.get("auth_key") != "signed":
                return httpx.Response(403, request=req)
            return ok(req, {"body": self.body})
        raise AssertionError(url)

    def youtube(self, req):
        vid = req.url.params["id"]
        if vid == "privateVid0":
            return ok(req, {"items": [{"snippet": {}, "status": {"privacyStatus": "unlisted"}}]})
        return ok(req, {"items": [{"id": vid, "snippet": {"title": "Neural networks", "channelTitle": "3B1B",
                                                          "publishedAt": "2017-10-05T00:00:00Z", "defaultAudioLanguage": "en",
                                                          "thumbnails": {"high": {"url": "https://i.ytimg.com/x.jpg"}}},
                                   "contentDetails": {"duration": "PT31M40S"}, "status": {"privacyStatus": "public"}}]})

    def gemini(self, body):
        part = body["contents"][0]["parts"][0]
        start, end = (int(part["video_metadata"][k][:-1]) for k in ("start_offset", "end_offset"))
        self.gemini_calls.append((start, end))
        action = self.gemini_plan.pop(0) if self.gemini_plan else "ok"
        req = httpx.Request("POST", "https://gemini")
        if action == "503":
            return httpx.Response(503, json={"error": {"message": "high demand"}}, request=req)
        if action == "quota":
            return httpx.Response(429, json={"error": {"message": "quota", "details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                 "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3600s"}]}}, request=req)
        finish = "MAX_TOKENS" if action == "max" else "STOP"
        # Relative timestamps for the middle slice: must be made absolute.
        base = 0 if action == "relative" else start
        lines = [f"[{(base + t) // 60:02d}:{(base + t) % 60:02d}] words at {start + t}" for t in range(0, end - start, 30)]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "\n".join(lines) + "\n---"}]},
                                                         "finishReason": finish}]}, request=req)

    def model(self, messages, schema, max_tokens=2400, **kw):
        system, prompt = messages[0]["content"], messages[1]["content"]
        if system.startswith(video.SECTION_SYSTEM[:20]):
            self.sections_seen.append(prompt.split("转录：\n", 1)[1])
            return json.dumps({"title": f"第{len(self.sections_seen)}段主题", "points": [f"要点{len(self.sections_seen)}"]}, ensure_ascii=False)
        self.overall_inputs.append(prompt)
        return json.dumps({"title": "x", "overview": "整段视频的中文概览。", "material_type": "视频",
                           "key_points": ["整体要点"], "topics": [], "topic_evidence": [], "tag_review": {},
                           "note_is_yours": True}, ensure_ascii=False)

    # ----- helpers -----
    def page(self):
        pages = [p for p in self.n.pages.values() if p["parent"].get("database_id") == "materials"]
        self.assertEqual(len(pages), 1)
        return pages[0], self.n.text(pages[0]["id"])

    def resume_all(self, key, rounds=10):
        for _ in range(rounds):
            job = store.get_video_job(key)
            if job.get("status") in ("done", "staged"):
                return job
            store.update_video_job(key, next_retry_at=0)   # the scheduled time has come
            video.resume(key)
        return store.get_video_job(key)

    # ----- tests -----
    def test_ids_and_aid_conversion(self):
        for url in ("https://youtu.be/aircAruvnKk?t=90", "https://www.youtube.com/watch?v=aircAruvnKk&list=x",
                    "https://m.youtube.com/shorts/aircAruvnKk", "https://www.youtube.com/live/aircAruvnKk"):
            self.assertEqual(video.youtube_id(url), "aircAruvnKk")
        self.assertEqual(video.youtube_id("https://www.youtube.com/@channel"), "")
        for bv in (BV, "BV1TAtwzTE1S", "BV17x411w7KC"):
            self.assertEqual(video.aid_to_bv(video.bv_to_aid(bv)), bv)
        self.assertEqual(video.bv_to_aid("BV17x411w7KC"), 170001)
        self.assertEqual(video.identify("https://youtu.be/aircAruvnKk?t=1m30s")["start"], 90)

    def test_track_choice_prefers_original_language(self):
        manual_en = {"lan": "en-US", "type": 0, "ai_type": 0}
        ai_zh = {"lan": "ai-zh", "type": 1, "ai_type": 0}
        ai_en_translated = {"lan": "ai-en", "type": 1, "ai_type": 1}
        manual_zh = {"lan": "zh-Hans", "type": 0, "ai_type": 0}
        self.assertIs(video.choose_track([ai_en_translated, ai_zh]), ai_zh)
        self.assertIs(video.choose_track([manual_en, manual_zh, ai_zh]), manual_zh)
        self.assertIs(video.choose_track([manual_en]), manual_en)
        self.assertIsNone(video.choose_track([ai_en_translated]))

    def test_bilibili_long_subtitles_fully_summarized_and_deduped(self):
        raw = "看完这个想再推一遍反向传播 https://b23.tv/abc123"
        text, url = pipeline._handle_url(self.msg, "https://b23.tv/abc123", "看完这个想再推一遍反向传播", lambda s: None, update_id=1)
        self.assertTrue(url, text)
        page, body = self.page()
        self.assertEqual(notion.prop_text(page, "处理状态"), "完成")
        self.assertEqual(notion.prop_text(page, "原始链接"), f"https://www.bilibili.com/video/{BV}/?p=2")
        self.assertEqual(notion.prop_text(page, "标题"), "李宏毅机器学习 · P2 下")
        self.assertIn("UP 主：李宏毅-机器学习", body)
        # Every subtitle line was in some section summary: nothing cut at 40,000.
        full = "\n".join(s["content"] for s in self.body)
        self.assertGreater(len(full), enrich.SUMMARY_INPUT_CHARS)
        seen = "\n".join(self.sections_seen)
        for i in (0, 1500, 2999):
            self.assertIn(LONG_LINE.format(i), seen)
            self.assertIn(LONG_LINE.format(i), body)
        self.assertGreater(len(self.sections_seen), 3)
        self.assertTrue(all("第1段主题" in s or "[" in s for s in self.overall_inputs))
        self.assertIn("第1段主题", self.overall_inputs[-1])
        for heading in ("中文概览", "章节要点", "完整转录", "转录来源与范围", "平台自动生成字幕", "我的想法"):
            self.assertIn(heading, body)
        self.assertIn(f"?p=2&t=", json.dumps(self.n.blocks, ensure_ascii=False))
        job = store.get_video_job(f"bilibili:{BV}:222")
        self.assertEqual(job["status"], "done")
        transcript = json.loads(self.files[job["transcript_uri"]])
        self.assertEqual(transcript["source_kind"], "platform_auto")
        self.assertEqual(len(transcript["segments"]), 3000)
        # Attached text is ignored, including genuine comments.
        self.assertEqual(len([p for p in self.n.pages.values() if p["parent"].get("database_id") == "thoughts"]), 0)
        # Other spellings of the same part: no new material or thought.
        before = len(self.sections_seen)
        again = {"chat": {"id": 7}, "message_id": 2}
        text, _ = pipeline._handle_url(again, f"https://www.bilibili.com/video/{BV}?p=2&t=120", "", lambda s: None, update_id=2)
        self.assertIn("已保存过", text)
        third = {"chat": {"id": 7}, "message_id": 3}
        text, _ = pipeline._handle_url(third, f"https://m.bilibili.com/video/{BV}?p=2", "第二遍看懂了激活函数那段", lambda s: None, update_id=3)
        self.assertIn("已保存过", text)
        self.assertEqual(len([p for p in self.n.pages.values() if p["parent"].get("database_id") == "materials"]), 1)
        self.assertEqual(len([p for p in self.n.pages.values() if p["parent"].get("database_id") == "thoughts"]), 0)
        self.assertEqual(len(self.sections_seen), before)
        # A different part is a different video.
        text, url = pipeline._handle_url({"chat": {"id": 7}, "message_id": 4}, f"https://www.bilibili.com/video/{BV}", "", lambda s: None, update_id=4)
        self.assertTrue(url)
        self.assertEqual(len([p for p in self.n.pages.values() if p["parent"].get("database_id") == "materials"]), 2)

    def test_bilibili_no_subtitle_waits_then_stages_without_thought(self):
        self.view_blocked = True   # metadata unavailable must not matter
        self.dm, self.need_login = {"subtitles": None}, False
        text, _ = pipeline._handle_url(self.msg, f"https://www.bilibili.com/video/{BV}?p=2", "这个视频以后要细看", lambda s: None, update_id=1)
        self.assertIn("自动再查", text)
        key = f"bilibili:{BV}:222"
        self.assertEqual(store.get_video_job(key)["status"], "waiting")
        self.assertEqual(self.scheduled[-1][1], video.BILI_WAIT_DELAYS[0])
        self.assertIsNone(store.get_staged(f"https://www.bilibili.com/video/{BV}/?p=2"))
        thoughts = [p for p in self.n.pages.values() if p["parent"].get("database_id") == "thoughts"]
        self.assertEqual(len(thoughts), 0)
        job = self.resume_all(key)
        self.assertEqual(job["status"], "staged")
        self.assertEqual([d for _, d in self.scheduled], list(video.BILI_WAIT_DELAYS))
        staged = store.get_staged(f"https://www.bilibili.com/video/{BV}/?p=2")
        self.assertIn("暂无字幕", staged["error"])
        self.assertFalse([p for p in self.n.pages.values() if p["parent"].get("database_id") == "materials"])
        # The subtitle appears later: /retry of the staged link completes it.
        self.dm = {"subtitles": [{"lan": "ai-zh", "type": 1, "ai_type": 0, "subtitle_url": "https://aisubtitle.hdslb.com/bfs/s.json?auth_key=signed"}]}
        self.body = subtitle_body(40)
        ok_, detail = pipeline._retry_staged(staged, lambda s: None)
        self.assertTrue(ok_, detail)
        self.assertEqual(store.get_video_job(key)["status"], "done")
        self.assertIsNone(store.get_staged(f"https://www.bilibili.com/video/{BV}/?p=2"))
        self.page()

    def test_bilibili_interface_failure_alerts_instead_of_no_subtitle(self):
        for case in ("code", "cross_check"):
            with self.subTest(case=case):
                self.db.data.clear(); self.scheduled.clear()
                if case == "code":
                    self.dm_code = -412
                else:
                    self.dm_code, self.dm, self.need_login = 0, {"subtitles": None}, True
                with self.assertLogs(video.log, "ERROR") as logs:
                    text, _ = pipeline._handle_url(self.msg, f"https://www.bilibili.com/video/{BV}?p=2", "", lambda s: None, update_id=1)
                    self.assertIn("自动重试", text)
                    job = self.resume_all(f"bilibili:{BV}:222")
                self.assertEqual(job["status"], "staged")
                self.assertTrue(job["alert"])
                self.assertIn("VIDEO_ALERT", "\n".join(logs.output))
                self.assertNotIn("暂无字幕", job["error"])
                self.assertEqual([d for _, d in self.scheduled], list(video.BILI_ERROR_DELAYS))

    def test_youtube_slices_quota_split_and_relative_stamps(self):
        self.gemini_plan = ["ok", "quota", "max", "relative", "ok", "ok"]
        text, _ = pipeline._handle_url(self.msg, "https://youtu.be/aircAruvnKk?t=42", "", lambda s: None, update_id=1)
        key = "youtube:aircAruvnKk"
        job = store.get_video_job(key)
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["quota_waits"], 1)
        # Google's retryDelay (3600 s) is ignored: wait for the Pacific-midnight reset.
        self.assertAlmostEqual(self.scheduled[-1][1], video.quota_reset(time.time()) - time.time(), delta=5)
        self.assertIn("今日 Gemini 免费额度已用完", job["error"])
        self.assertEqual(len(job["chunks"]), 1)
        # The same quota day stays spent: no request goes out.
        store.update_video_job(key, next_retry_at=0); video.resume(key)
        self.assertEqual(len(self.gemini_calls), 2)
        self.day[0] = "2026-10-17"
        job = self.resume_all(key)
        self.assertEqual(job["status"], "done")
        # Done slices are never re-requested; the truncated last slice was halved.
        self.assertEqual(self.gemini_calls, [(0, 900), (900, 1800), (900, 1800), (900, 1350), (1350, 1800), (1800, 1900)])
        transcript = json.loads(self.files[job["transcript_uri"]])
        self.assertEqual(transcript["source_kind"], "model_transcript")
        starts = [s["start"] for s in transcript["segments"]]
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(any(s["start"] >= 900 and "words at 900" in s["text"] for s in transcript["segments"]))
        page, body = self.page()
        self.assertEqual(notion.prop_text(page, "原始链接"), "https://www.youtube.com/watch?v=aircAruvnKk")
        self.assertIn("模型生成转录", body)
        self.assertIn("近似值", body)
        self.assertIn("分享时的跳转起点：00:42", body)
        self.assertIn("频道：3B1B", body)

    def test_youtube_overload_backs_off_then_stages_without_paid_switch(self):
        self.gemini_plan = ["503"] * 100
        pipeline._handle_url(self.msg, "https://www.youtube.com/watch?v=aircAruvnKk", "", lambda s: None, update_id=1)
        job = self.resume_all("youtube:aircAruvnKk", rounds=20)
        self.assertEqual(job["status"], "staged")
        self.assertEqual([d for _, d in self.scheduled], list(video.GEMINI_DELAYS))
        self.assertIn("暂时不可用", store.get_staged("https://www.youtube.com/watch?v=aircAruvnKk")["error"])
        self.assertEqual({c for c in self.gemini_calls}, {(0, 900)})
        # One request per round, no immediate retries: each 503 spends a daily request.
        self.assertEqual(len(self.gemini_calls), 1 + len(video.GEMINI_DELAYS))
        self.assertEqual(self.db.data["gemini_quota"][f"{config.GEMINI_MODEL}:2026-10-16"]["count"], len(self.gemini_calls))

    def test_daily_request_budget_stops_before_sending(self):
        self.p(config, "GEMINI_DAILY_REQUESTS", 2)
        text, _ = pipeline._handle_url(self.msg, "https://www.youtube.com/watch?v=aircAruvnKk", "", lambda s: None, update_id=1)
        key = "youtube:aircAruvnKk"
        for _ in range(3):   # rounds until the day's budget stops it
            job = store.get_video_job(key)
            if "免费额度" in (job.get("error") or ""):
                break
            store.update_video_job(key, next_retry_at=0); video.resume(key)
        job = store.get_video_job(key)
        # 31:40 is 3 slices; the third is not requested once 2 are used today.
        self.assertEqual(self.gemini_calls, [(0, 900), (900, 1800)])
        self.assertEqual(job["status"], "pending")
        self.assertRegex(job["error"], r"今日 Gemini 免费额度已用完，(今天|明天|\d\d-\d\d) \d\d:\d\d 继续")
        self.assertEqual(sorted(job["chunks"]), ["0-900", "900-1800"])
        self.assertAlmostEqual(self.scheduled[-1][1], video.quota_reset(time.time()) - time.time(), delta=5)
        self.day[0] = "2026-10-17"
        job = self.resume_all(key)
        self.assertEqual(job["status"], "done")
        self.assertEqual(self.gemini_calls[-1], (1800, 1900))

    def test_quota_day_and_reset_follow_pacific_midnight(self):
        self.p(video.config, 'USER_TIMEZONE', 'America/Chicago')
        utc = lambda s: datetime.datetime.fromisoformat(s + "+00:00").timestamp()
        self.assertEqual(REAL_QUOTA_DAY(utc("2026-10-17T06:59:59")), "2026-10-16")
        self.assertEqual(REAL_QUOTA_DAY(utc("2026-10-17T07:00:00")), "2026-10-17")
        # 10-16 17:11 UTC (the observed 429) resumes 10-17 07:05 UTC, i.e. 02:05 in Chicago.
        self.assertEqual(video.quota_reset(utc("2026-10-16T17:11:34")), utc("2026-10-17T07:05:00"))
        self.assertEqual(str(video.quota_spent(utc("2026-10-16T17:11:34"))), "今日 Gemini 免费额度已用完，明天 02:05 继续")
        self.assertEqual(str(video.quota_spent(utc("2026-10-17T06:30:00"))), "今日 Gemini 免费额度已用完，今天 02:05 继续")
        # Across the end of daylight saving time (2026-11-01) the reset moves to 08:05 UTC.
        self.assertEqual(video.quota_reset(utc("2026-11-01T05:00:00")), utc("2026-11-01T07:05:00"))
        self.assertEqual(video.quota_reset(utc("2026-11-01T12:00:00")), utc("2026-11-02T08:05:00"))

    def test_private_youtube_staged_at_once(self):
        text, _ = pipeline._handle_url(self.msg, "https://youtu.be/privateVid0", "", lambda s: None, update_id=1)
        self.assertIn("公开", text)
        self.assertFalse(self.gemini_calls)
        self.assertTrue(store.get_staged("https://www.youtube.com/watch?v=privateVid0"))

    def test_lost_create_response_adopts_page(self):
        self.body = subtitle_body(30)
        self.n.lose_create = True
        with self.assertRaises(RuntimeError):
            pipeline._handle_url(self.msg, f"https://www.bilibili.com/video/{BV}?p=2", "", lambda s: None, update_id=1)
        text, url = pipeline._handle_url(self.msg, f"https://www.bilibili.com/video/{BV}?p=2", "", lambda s: None, update_id=1)
        self.assertTrue(url, text)
        page, body = self.page()
        self.assertEqual(notion.prop_text(page, "处理状态"), "完成")
        self.assertEqual(body.count("完整转录"), 1)

    def test_queue_handoff_and_busy_job_keeps_thought(self):
        self.p(config, "SERVICE_URL", "https://svc"); self.p(config, "TASKS_SA_EMAIL", "sa@test")
        self.body = subtitle_body(30)
        text, url = pipeline._handle_url(self.msg, f"https://www.bilibili.com/video/{BV}?p=2", "", lambda s: None, update_id=1)
        self.assertIn("字幕已取得", text)
        self.assertFalse(url); self.assertFalse(self.sections_seen and self.overall_inputs)
        key = f"bilibili:{BV}:222"
        owner = store.claim_video_job(key)
        text, _ = pipeline._handle_url({"chat": {"id": 7}, "message_id": 5}, f"https://www.bilibili.com/video/{BV}?p=2",
                                       "转发给组里看看，重点是第二个例子", lambda s: None, update_id=5)
        self.assertIn("正在处理", text)
        self.assertEqual(len([p for p in self.n.pages.values() if p["parent"].get("database_id") == "thoughts"]), 0)
        store.release_video_job(key, owner)
        self.assertTrue(video.resume(key)["completed"])
        text, _ = pipeline._handle_url(self.msg, "https://youtu.be/aircAruvnKk", "", lambda s: None, update_id=6)
        self.assertIn("已开始 YouTube 转录", text)
        self.assertFalse(self.gemini_calls)

    def test_sections_cover_every_segment_once(self):
        segs = [{"start": float(i), "end": float(i + 1), "text": "字" * (i % 50 + 1)} for i in range(2000)]
        parts = video.sections(segs, 3000)
        self.assertEqual(sum(len(p["segments"]) for p in parts), len(segs))
        self.assertTrue(all(len(p["text"]) <= 3000 for p in parts))
        self.assertEqual(video.parse_lines("[00:05] a\ncontinued\n---\n[1:00:01] b", 0, 4000),
                         [{"start": 5.0, "text": "a continued"}, {"start": 3601.0, "text": "b"}])

    def test_parse_lines_splits_inline_timestamps(self):
        # A whole slice on one line, as gemini-3.8-flash returned for Karpathy 0-900 (10-15).
        rows = video.parse_lines("[00:00] Hi everyone. [00:01] So by now [00:09] (no speech) [14:58] kind of a", 0, 900)
        self.assertEqual([r["start"] for r in rows], [0.0, 1.0, 9.0, 898.0])
        self.assertTrue(video._NO_SPEECH.match(rows[2]["text"]))
        self.assertEqual(rows[3]["text"], "kind of a")
        # Relative stamps on one line are still made absolute.
        rows = video.parse_lines("[00:00] a [05:00] b", 2700, 3600)
        self.assertEqual([r["start"] for r in rows], [2700.0, 3000.0])


if __name__ == "__main__":
    unittest.main()
