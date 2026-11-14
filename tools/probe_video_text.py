"""Read-only video caption feasibility probe. No audio, ASR, model or Notion calls.

Install tools/video-probe-requirements.txt in an isolated venv. With no flags,
prints one JSON record per adapter/sample. --output saves full transcripts.
--local-bili-session uses ONLY an existing bilibili-cli session in memory;
never extracts browser cookies, refreshes credentials, or sends them to GCP.
Each adapter runs in a child process with a hard timeout.
"""
import argparse
import concurrent.futures
import hashlib
import html
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit

SAMPLES = [
    ("yt_manual_en", "https://www.youtube.com/watch?v=aircAruvnKk", "en"),
    ("yt_transformers", "https://www.youtube.com/watch?v=wjZofJX0v4M", "en"),
    ("yt_long_lecture", "https://www.youtube.com/watch?v=kCc8FmEb1nY", "en"),
    ("yt_llm_intro", "https://youtu.be/zjkBMFhNj_g?t=60", "en"),
    ("yt_zh_recent", "https://www.youtube.com/watch?v=2yCfJAhnimk", "zh"),
    ("yt_zh_explainer", "https://www.youtube.com/watch?v=wu0zS1AuI_E", "zh"),
    ("yt_short_caption_check", "https://www.youtube.com/watch?v=jNQXAC9IVRw", "en"),
    ("bili_ml_p1", "https://www.bilibili.com/video/BV1DTxXzUEvW?p=1", "zh"),
    ("bili_ml_p2", "https://www.bilibili.com/video/BV1DTxXzUEvW?p=2", "zh"),
    ("bili_ml_2025", "https://www.bilibili.com/video/BV1TAtwzTE1S?p=1", "zh"),
    ("bili_music", "https://www.bilibili.com/video/BV1GJ411x7h7", "en"),
    ("bili_talk", "https://www.bilibili.com/video/BV13x41117TL", "zh"),
    ("bili_op_p2", "https://www.bilibili.com/video/BV1bK411W797?p=2", "ja"),
]


def bili_cookies(enabled):
    if not enabled:
        return {}
    p = Path.home() / ".bilibili-cli/credential.json"
    d = json.loads(p.read_text()) if p.exists() else {}
    return {out: d[key] for key, out in (
        ("sessdata", "SESSDATA"), ("bili_jct", "bili_jct"),
        ("buvid3", "buvid3"), ("buvid4", "buvid4"),
        ("dedeuserid", "DedeUserID")) if d.get(key)}


def stats(segments, duration=None):
    text = "\n".join(s["text"] for s in segments)
    bad = [i for i, s in enumerate(segments) if
           s["start"] < 0 or s["end"] < s["start"] or
           (i and s["start"] < segments[i - 1]["start"])]
    end = max((s["end"] for s in segments), default=0)
    return {"segment_count": len(segments), "text_chars": len(text),
            "first_start": segments[0]["start"] if segments else None,
            "last_end": end, "invalid_timestamps": bad,
            "last_end_over_duration": round(end / duration, 4) if duration else None,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest()}


def missing_track_status(warnings):
    """A bot-check warning must never be converted into 'video has no captions'."""
    text = "\n".join(warnings).lower()
    if "not a bot" in text:
        return "bot_check"
    if "logged in" in text:
        return "login_required"
    return "no_matching_tracks"


def ytt(url, language):
    import requests
    from youtube_transcript_api import YouTubeTranscriptApi
    class BoundedSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", 12)
            return super().request(*args, **kwargs)
    parts = urlsplit(url)
    vid = parts.path.strip("/") if parts.hostname == "youtu.be" else parse_qs(parts.query)["v"][0]
    tracks = list(YouTubeTranscriptApi(http_client=BoundedSession()).list(vid))
    matching = [t for t in tracks if t.language_code.startswith(language)]
    if not matching:
        return {"status": "requested_language_missing", "tracks": [t.language_code for t in tracks]}
    track = sorted(matching, key=lambda t: t.is_generated)[0]
    raw = track.fetch().to_raw_data()
    segments = [{"start": s["start"], "end": s["start"] + s["duration"], "text": s["text"]} for s in raw]
    return {"status": "ok" if segments else "empty", "video_id": vid,
            "source_kind": "platform_auto" if track.is_generated else "platform_manual",
            "language": track.language_code, "segments": segments, "raw": raw,
            "tracks": [{"language": t.language_code, "auto": t.is_generated} for t in tracks],
            **stats(segments)}


def bili_api(url, language, session):
    import requests
    bvid = re.search(r"BV[0-9A-Za-z]{10}", url)[0]
    page = int(parse_qs(urlsplit(url).query).get("p", [1])[0])
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"})
    s.cookies.update(bili_cookies(session))
    def get_json(url, **kwargs):
        response = s.get(url, timeout=12, **kwargs)
        response.raise_for_status()
        return response.json()
    meta = get_json("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid})
    if meta.get("code"):
        return {"status": "metadata_api_error", "api_code": meta.get("code"), "message": meta.get("message")}
    d = meta["data"]
    part = d["pages"][page - 1]
    base = {"video_id": bvid, "part_id": part["cid"], "page": page,
            "title": d["title"], "part_title": part["part"], "duration": part["duration"],
            "parts_count": len(d["pages"])}
    player = get_json("https://api.bilibili.com/x/player/wbi/v2", params={"bvid": bvid, "cid": part["cid"]})
    if player.get("code"):
        return {**base, "status": "player_api_error", "api_code": player.get("code"), "message": player.get("message")}
    p = player["data"]
    tracks = p.get("subtitle", {}).get("subtitles", [])
    if not tracks:
        return {**base, "status": "login_required" if p.get("need_login_subtitle") else "no_tracks_reported",
                "need_login_subtitle": bool(p.get("need_login_subtitle")), "tracks": []}
    matching = [t for t in tracks if language in t["lan"]]
    if not matching:
        return {**base, "status": "requested_language_missing",
                "tracks": [{"language": t["lan"], "label": t.get("lan_doc")} for t in tracks]}
    track = sorted(matching, key=lambda t: t["lan"].startswith("ai-"))[0]
    u = track["subtitle_url"]
    u = "https:" + u if u.startswith("//") else u
    raw = get_json(u)
    segments = [{"start": i["from"], "end": i["to"], "text": i["content"]} for i in raw.get("body", [])]
    return {**base, "status": "ok" if segments else "empty", "language": track["lan"],
            "source_kind": "platform_auto" if track["lan"].startswith("ai-") else "platform_manual",
            "tracks": [{"language": t["lan"], "label": t.get("lan_doc")} for t in tracks],
            "segments": segments, "raw": raw, **stats(segments, part["duration"])}


def ytdlp(url, language, session):
    import yt_dlp
    from yt_dlp.cookies import YoutubeDLCookieJar
    from http.cookiejar import Cookie
    class Log:
        def __init__(self): self.messages = []
        def debug(self, message): pass
        def info(self, message): pass
        def warning(self, message): self.messages.append(message)
        def error(self, message): self.messages.append(message)
    log = Log()
    opts = {"skip_download": True, "noplaylist": True, "quiet": True, "writesubtitles": True,
            "socket_timeout": 12, "retries": 0, "extractor_retries": 0,
            "logger": log, "ignore_no_formats_error": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        if session and "bilibili.com" in url:
            jar = YoutubeDLCookieJar()
            for name, value in bili_cookies(True).items():
                jar.set_cookie(Cookie(0, name, value, None, False, ".bilibili.com", True, True,
                                      "/", True, True, None, True, None, None, {}))
            ydl.cookiejar = jar
        info = ydl.extract_info(url, download=False)
        if info.get("entries"):
            return {"status": "unexpected_playlist", "warnings": log.messages}
        tracks = info.get("subtitles") or {}
        autos = info.get("automatic_captions") or {}
        base = {"title": info.get("title"), "duration": info.get("duration"),
                "extractor_video_id": info.get("id"), "tracks": list(tracks),
                "auto_tracks": list(autos), "warnings": log.messages}
        choices = [(key, vals, False) for key, vals in tracks.items() if key != "danmaku" and language in key]
        choices += [(key, vals, True) for key, vals in autos.items() if key != "danmaku" and language in key and "orig" in key]
        if not choices:
            choices += [(key, vals, True) for key, vals in autos.items() if key != "danmaku" and key == language]
        if not choices:
            return {**base, "status": missing_track_status(log.messages)}
        key, formats, auto = choices[0]
        f = sorted(formats, key=lambda f: (f.get("ext") != "json3", f.get("ext") != "srt"))[0]
        data = f.get("data")
        if data is None:
            with ydl.urlopen(f["url"]) as response:
                data = response.read(8 * 1024 * 1024).decode()
        segments = []
        if f.get("ext") == "json3":
            raw = json.loads(data)
            for e in raw.get("events", []):
                txt = "".join(i.get("utf8", "") for i in e.get("segs", [])).strip()
                if txt:
                    start = e.get("tStartMs", 0) / 1000
                    segments.append({"start": start, "end": start + e.get("dDurationMs", 0) / 1000, "text": txt})
        else:
            raw = data
            def seconds(t):
                h, m, sec = t.replace(",", ".").split(":")
                return int(h) * 3600 + int(m) * 60 + float(sec)
            for m in re.finditer(r"(\d{2}:\d{2}:\d{2}[,.]\d+) --> (\d{2}:\d{2}:\d{2}[,.]\d+)[^\n]*\n(.*?)(?=\n\s*\n|\Z)", data, re.S):
                segments.append({"start": seconds(m[1]), "end": seconds(m[2]), "text": html.unescape(re.sub(r"<[^>]+>", "", m[3])).strip()})
        return {**base, "status": "ok" if segments else "empty_or_unparsed", "language": key,
                "source_kind": "platform_auto" if auto or key.startswith("ai-") else "platform_manual",
                "segments": segments, "raw": raw, **stats(segments, info.get("duration"))}


def worker(sample, adapter, session):
    start = time.monotonic()
    try:
        result = {"ytt": lambda: ytt(sample[1], sample[2]),
                  "yt_dlp": lambda: ytdlp(sample[1], sample[2], session),
                  "bili_player_api": lambda: bili_api(sample[1], sample[2], session)}[adapter]()
    except Exception as exc:
        # Only retain type: exception strings can include signed URLs or cookies.
        message = str(exc).lower()
        category = next((label for needle, label in (
            ("not a bot", "bot_check"), ("requestblocked", "request_blocked"),
            ("ipblocked", "ip_blocked"), ("http error 412", "http_412"),
            ("http error 429", "rate_limited"), ("http error 403", "http_403"),
            ("video unavailable", "unavailable"), ("failed to resolve", "dns_error"),
            ("timed out", "network_timeout")) if needle in message), "unclassified")
        result = {"status": "error", "error_type": type(exc).__name__, "error_category": category}
        response = getattr(exc, "response", None)
        if response is not None:
            result["http_status"] = response.status_code
            result["error_category"] = f"http_{response.status_code}"
    return {"sample": sample[0], "url": sample[1], "adapter": adapter,
            "scope": os.environ.get("PROBE_SCOPE", "local"),
            "bili_session": session and "bilibili.com" in sample[1],
            "elapsed_s": round(time.monotonic() - start, 3), **result}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path)
    p.add_argument("--local-bili-session", action="store_true")
    p.add_argument("--worker", nargs=2, metavar=("SAMPLE", "ADAPTER"))
    p.add_argument("--only", nargs="*")
    p.add_argument("--timeout", type=int, default=45)
    a = p.parse_args()
    if a.worker:
        sample = next(s for s in SAMPLES if s[0] == a.worker[0])
        print(json.dumps(worker(sample, a.worker[1], a.local_bili_session), ensure_ascii=False))
        return
    if a.output:
        a.output.mkdir(parents=True, exist_ok=True)
    versions = {n: importlib.metadata.version(n) for n in ("youtube-transcript-api", "yt-dlp")}
    print(json.dumps({"event": "versions", "versions": versions, "python": sys.version.split()[0]}), flush=True)
    jobs = [(s, adapter) for s in SAMPLES if not a.only or s[0] in a.only
            for adapter in (("ytt", "yt_dlp") if "youtu" in s[1] else ("bili_player_api", "yt_dlp"))]
    def run(job):
        s, adapter = job
        cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", s[0], adapter]
        if a.local_bili_session: cmd.append("--local-bili-session")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout)
            r = json.loads(proc.stdout) if proc.returncode == 0 else {"sample": s[0], "adapter": adapter, "status": "worker_error"}
        except subprocess.TimeoutExpired:
            r = {"sample": s[0], "adapter": adapter, "status": "timeout", "elapsed_s": a.timeout}
        if a.output:
            (a.output / f"{s[0]}-{adapter}.json").write_text(json.dumps(r, ensure_ascii=False, indent=2))
            if r.get("segments"):
                (a.output / f"{s[0]}-{adapter}.txt").write_text("\n".join(f'[{i["start"]:.3f}–{i["end"]:.3f}] {i["text"]}' for i in r["segments"]))
        return {k: v for k, v in r.items() if k not in ("raw", "segments")}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        for r in pool.map(run, jobs):
            print(json.dumps(r, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
