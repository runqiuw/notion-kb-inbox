"""Video links: platform text -> sectioned summary of the whole transcript -> one page.

B站: anonymous subtitle chain (BV -> aid locally, pagelist -> cid, dm/view ->
tracks, signed subtitle JSON). No login cookie, no audio download. A video with
no original-language track yet waits for B站's delayed AI subtitles, then is
staged. An interface failure is staged with an alert -- it is never mistaken
for "this video has no subtitles".

YouTube: Cloud Run cannot read YouTube itself, so the Gemini free tier reads the
URL and transcribes it verbatim in fixed time slices (model_transcript, with
approximate timestamps). Overload, rate limits and quota exhaustion back off and
finally stage; there is no paid fallback.

Durable like image_batches: Firestore video_jobs/{entity} holds the stage and
checkpoints, GCS holds transcripts and per-section results, and each Cloud Tasks
round on the video queue does what fits in the request budget.
"""
import datetime
import hashlib
import json
import logging
import re
import time
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx

from . import config, enrich, storage, store, tasks, work_budget

log = logging.getLogger(__name__)
VERSION = 1

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3/videos"

# B站 AI subtitles appear some time after upload (3 of 18 new uploads gained one
# within ~1.5 h in the 10-15 sample). Check a few times, then stage.
BILI_WAIT_DELAYS = (1800, 7200, 21600, 86400)
# An interface failure is not a missing subtitle: a short retry, then stage and alert.
BILI_ERROR_DELAYS = (300, 1800)
# Gemini free tier 503s often, and every request -- a 503 too -- counts toward
# the 20 a day (10-16: 21 503s in a row spent a whole day). So one request per
# round and long gaps; the tail covers a daily reset.
GEMINI_DELAYS = (900, 3600, 4 * 3600, 12 * 3600)
LLM_DELAYS = (60, 300, 900, 3600)

# The free tier allows only 20 requests a day per model (measured 10-15), so
# slices are long: 15 min was validated (183 s, 4.1% word diff) and gives ~5 h
# of video a day. A slice needs more than the 300 s other requests get, so
# video rounds run on their own queue with a longer budget.
CHUNK_SECONDS = 900
MIN_CHUNK_SECONDS = 120
GEMINI_TIMEOUT = 600.0
ROUND_SECONDS = 840
QUOTA_WAITS = 3              # daily-quota resets to wait through before staging
QUOTA_RESET_GRACE = 300      # resume this long after the Pacific-midnight reset
SECTION_CHARS = 12000        # transcript characters per section summary
DIGEST_LIMIT = enrich.SUMMARY_INPUT_CHARS
PARAGRAPH_SECONDS = 60       # transcript lines grouped per Notion paragraph

GEMINI_PROMPT = """Transcribe the spoken audio of this video verbatim, in the original spoken language{lang}.
Rules:
- Output ONLY transcript lines, each formatted as [MM:SS] text  (use [H:MM:SS] past one hour). Timestamps are absolute positions in the full video.
- Do not summarize, translate, paraphrase, skip, or add commentary, headings, or descriptions of visuals.
- If a stretch has no speech (music, silence), write one line like [MM:SS] (no speech) and continue.
- Cover the whole segment from {start}s to {end}s through to the end."""

SECTION_SYSTEM = """你在为中文个人知识库整理一段视频转录。只根据给出的这一段转录：
- title：这一段的中文小标题，8-20 字，概括主题
- points：2-6 条中文要点，每条 1-2 句，保留术语、数字、例子；说话人的观点归于说话人
不补充转录里没有的信息，不写评价或建议，不以用户口吻写。转录可能有识别错误：
听不清或疑似识别错误的人名、术语照录并注明（疑似识别错误）。
只输出 JSON：{"title": "小标题", "points": ["要点"]}"""

SECTION_SCHEMA = {"name": "video_section", "schema": {
    "type": "object",
    "properties": {"title": {"type": "string"},
                   "points": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
    "required": ["title", "points"], "additionalProperties": False}}

SOURCE_LABEL = {
    "platform_manual": "平台人工字幕",
    "platform_auto": "平台自动生成字幕（B 站 AI 字幕）",
    "model_transcript": "模型生成转录（Gemini 读取 YouTube 视频，非平台字幕）",
}


class Pending(RuntimeError):
    """Try again later. `kind` picks the back-off schedule; None = just continue."""

    def __init__(self, message, kind=None, alert=False, delay=0):
        super().__init__(message)
        self.kind, self.alert, self.delay = kind, alert, delay


class Waiting(RuntimeError):
    """B站 has no original-language track yet."""


class GiveUp(RuntimeError):
    """Will not fix itself: stage now."""

    def __init__(self, message, alert=False):
        super().__init__(message)
        self.alert = alert


class Busy(RuntimeError):
    pass


# ---------- identity ----------

_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_BV = re.compile(r"BV[0-9A-Za-z]{10}")
_AV = re.compile(r"(?:^|/)av(\d+)", re.I)
ALPHABET = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
_XOR, _MASK = 23442827791579, (1 << 51) - 1


def bv_to_aid(bvid: str) -> int:
    b = list(bvid)
    b[3], b[9] = b[9], b[3]
    b[4], b[7] = b[7], b[4]
    n = 0
    for c in b[3:]:
        n = n * 58 + ALPHABET.index(c)
    return (n & _MASK) ^ _XOR


def aid_to_bv(aid: int) -> str:
    b = list("BV1000000000")
    n, i = ((1 << 51) | aid) ^ _XOR, len(b) - 1
    while n:
        b[i] = ALPHABET[n % 58]
        n //= 58
        i -= 1
    b[3], b[9] = b[9], b[3]
    b[4], b[7] = b[7], b[4]
    return "".join(b)


def _seconds(value: str) -> int:
    """t=90, t=1m30s, t=1h2m3s -> seconds; anything else -> 0."""
    value = (value or "").strip().lower()
    if value.isdigit():
        return int(value)
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", value)
    if not m or not any(m.groups()):
        return 0
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def youtube_id(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    path = [p for p in parts.path.split("/") if p]
    vid = ""
    if host == "youtu.be" and path:
        vid = path[0]
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        if path[:1] == ["watch"] or not path:
            vid = (parse_qs(parts.query).get("v") or [""])[0]
        elif len(path) > 1 and path[0] in ("shorts", "live", "embed", "v", "e"):
            vid = path[1]
    return vid if _YT_ID.match(vid or "") else ""


def _client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
                        timeout=work_budget.timeout(15.0, reserve=5), follow_redirects=True)


def _bili_json(client: httpx.Client, url: str, **params) -> dict:
    # httpx replaces a URL's existing query string whenever params is passed,
    # even empty -- that silently drops a signed subtitle URL's auth_key.
    try:
        r = client.get(url, params=params) if params else client.get(url)
    except httpx.HTTPError as e:
        raise Pending(f"B 站接口网络异常：{type(e).__name__}", "bili", alert=True) from None
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        raise Pending(f"B 站接口 HTTP {r.status_code}（{urlsplit(url).path}）", "bili", alert=True)
    try:
        return r.json()
    except ValueError:
        raise Pending(f"B 站接口返回非 JSON（{urlsplit(url).path}）", "bili", alert=True) from None


_GONE = {-404: "视频不存在或已删除", 62002: "稿件不可见", 62004: "稿件审核中", 62012: "稿件仅 UP 主自己可见"}


def identify(url: str) -> dict:
    """The video this link is about: platform, entity key, canonical URL.

    B站 needs the network (short links, and the part's cid comes from
    pagelist); YouTube does not.
    """
    vid = youtube_id(url)
    if vid:
        q = parse_qs(urlsplit(url).query)
        return {"platform": "YouTube", "key": f"youtube:{vid}", "video_id": vid,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "start": _seconds((q.get("t") or q.get("start") or [""])[0])}
    host = urlsplit(url).netloc.lower()
    if not any(h in host for h in ("bilibili.com", "b23.tv", "bili2233.cn")):
        raise GiveUp("无法识别的视频链接")
    with _client() as client:
        resolved = url
        if "bilibili.com" not in host:
            try:
                resolved = str(client.get(url).url)
            except httpx.HTTPError as e:
                raise Pending(f"B 站短链解析失败：{type(e).__name__}", "bili", alert=True) from None
        parts = urlsplit(resolved)
        q = parse_qs(parts.query)
        m = _BV.search(parts.path) or _BV.search(parts.query)
        if m:
            bvid = m.group(0)
        else:
            av = _AV.search(parts.path)
            if not av:
                raise GiveUp("暂只支持 B 站普通视频（BV/av 号）链接；番剧、课程、直播等未接入")
            bvid = aid_to_bv(int(av.group(1)))
        try:
            page = max(1, int((q.get("p") or ["1"])[0]))
        except ValueError:
            page = 1
        client.get("https://www.bilibili.com/")   # anonymous device cookie only
        data = _bili_json(client, "https://api.bilibili.com/x/player/pagelist", bvid=bvid)
    code = data.get("code")
    if code in _GONE:
        raise GiveUp(f"B 站：{_GONE[code]}")
    if code or not data.get("data"):
        raise Pending(f"B 站 pagelist 返回 code {code}", "bili", alert=True)
    parts_list = data["data"]
    if page > len(parts_list):
        raise GiveUp(f"B 站视频只有 {len(parts_list)} 个分 P，没有第 {page} P")
    part = parts_list[page - 1]
    return {"platform": "B站", "key": f"bilibili:{bvid}:{part['cid']}", "bvid": bvid,
            "aid": bv_to_aid(bvid), "cid": part["cid"], "page": page, "pages": len(parts_list),
            "part_title": part.get("part") or "", "duration": part.get("duration") or 0,
            "url": f"https://www.bilibili.com/video/{bvid}/" + (f"?p={page}" if page > 1 else ""),
            "start": _seconds((q.get("t") or [""])[0])}


def jump_url(ref: dict, seconds: float) -> str:
    t = int(seconds)
    if ref["platform"] == "YouTube":
        return f"{ref['url']}&t={t}s"
    return f"https://www.bilibili.com/video/{ref['bvid']}/?p={ref['page']}&t={t}"


# ---------- metadata ----------

def _iso_duration(value: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def youtube_meta(ref: dict) -> dict:
    if not config.GEMINI_API_KEY:
        raise GiveUp("未配置 Gemini/YouTube 密钥，YouTube 视频暂不能转录", alert=True)
    try:
        r = httpx.get(YOUTUBE_API, params={"part": "snippet,contentDetails,status",
                                           "id": ref["video_id"], "key": config.GEMINI_API_KEY},
                      timeout=work_budget.timeout(20.0, reserve=5))
    except httpx.HTTPError as e:
        raise Pending(f"YouTube 元数据请求失败：{type(e).__name__}", "gemini") from None
    if r.status_code != 200:
        raise Pending(f"YouTube 元数据 HTTP {r.status_code}", "gemini", alert=r.status_code in (400, 403))
    items = r.json().get("items") or []
    if not items:
        raise GiveUp("YouTube 视频不存在、私有或已删除")
    item = items[0]
    s, status = item.get("snippet") or {}, item.get("status") or {}
    if status.get("privacyStatus") != "public":
        raise GiveUp(f"Gemini 只能读取公开 YouTube 视频（当前为 {status.get('privacyStatus') or '未知'}）")
    if s.get("liveBroadcastContent") in ("live", "upcoming"):
        raise GiveUp("YouTube 直播或预告暂不支持，结束后再发")
    thumbs = s.get("thumbnails") or {}
    thumb = next((thumbs[k]["url"] for k in ("maxres", "standard", "high", "medium", "default") if k in thumbs), "")
    return {"title": s.get("title") or "", "author": s.get("channelTitle") or "",
            "published": (s.get("publishedAt") or "")[:10],
            "duration": _iso_duration((item.get("contentDetails") or {}).get("duration", "")),
            "language": s.get("defaultAudioLanguage") or s.get("defaultLanguage") or "",
            "thumbnail": thumb}


def bilibili_meta(ref: dict, client: httpx.Client) -> dict:
    meta = {"title": ref["part_title"], "author": "", "published": "",
            "duration": ref["duration"], "language": "", "thumbnail": ""}
    # The plain view endpoint 412s from some egress (home and, on 10-15, Cloud
    # Run); the wbi path answers the same data unsigned. Title metadata is a
    # nicety -- the subtitle chain never depends on it.
    view = {}
    for path in ("x/web-interface/wbi/view", "x/web-interface/view"):
        try:
            view = _bili_json(client, "https://api.bilibili.com/" + path, bvid=ref["bvid"])
        except Pending as e:
            log.warning("bilibili %s unavailable for %s: %s", path, ref["bvid"], e)
            continue
        if not view.get("code") and view.get("data"):
            break
    d = view.get("data") or {}
    if view.get("code") or not d:
        return meta
    title = d.get("title") or ref["part_title"]
    if ref.get("pages", 1) > 1 and ref["part_title"] and ref["part_title"] != title:
        title = f"{title} · P{ref['page']} {ref['part_title']}"
    meta.update(title=title, author=(d.get("owner") or {}).get("name") or "",
                published=(datetime.datetime.fromtimestamp(d["pubdate"], datetime.timezone.utc).strftime("%Y-%m-%d")
                           if d.get("pubdate") else ""),
                thumbnail=(d.get("pic") or "").replace("http://", "https://", 1))
    return meta


# ---------- B站 subtitles ----------

def choose_track(tracks: list[dict]) -> dict | None:
    """Original-language text only: an uploader track, else B站's own ASR.

    `ai_type` 1 is B站's machine translation of its ASR -- text in a language
    nobody spoke -- so a video with only those has no usable track.
    """
    ai_original = [t for t in tracks if (t.get("type") == 1 or t["lan"].startswith("ai-"))
                   and not t.get("ai_type")]
    manual = [t for t in tracks if t.get("type", 0) == 0 and not t["lan"].startswith("ai-")]
    if manual:
        spoken = ai_original[0]["lan"][3:] if ai_original else ""
        same = [t for t in manual if spoken and t["lan"].lower().startswith(spoken.lower())]
        zh = [t for t in manual if t["lan"].lower().startswith("zh")]
        return (same or zh or manual)[0]
    return ai_original[0] if ai_original else None


def bilibili_transcript(ref: dict, job: dict) -> dict:
    with _client() as client:
        client.get("https://www.bilibili.com/")
        if not job.get("meta"):
            job["meta"] = bilibili_meta(ref, client)
        dm = _bili_json(client, "https://api.bilibili.com/x/v2/dm/view", type=1, oid=ref["cid"], pid=ref["aid"])
        if dm.get("code"):
            raise Pending(f"B 站 dm/view 返回 code {dm.get('code')}", "bili", alert=True)
        subtitle = (dm.get("data") or {}).get("subtitle")
        if not isinstance(subtitle, dict) or "subtitles" not in subtitle:
            raise Pending("B 站 dm/view 响应缺少字幕字段，接口可能已变化", "bili", alert=True)
        tracks = subtitle["subtitles"] or []   # null when the video has none
        track = choose_track(tracks)
        if not track:
            if not tracks:
                # Cross-check: the player still says subtitles exist (they just
                # need a login there) -> dm/view stopped reporting them.
                try:
                    player = _bili_json(client, "https://api.bilibili.com/x/player/v2", aid=ref["aid"], cid=ref["cid"])
                    if (player.get("data") or {}).get("need_login_subtitle"):
                        raise Pending("B 站 dm/view 无字幕轨，但播放器接口显示有字幕；接口可能已变化",
                                      "bili", alert=True)
                except Pending as e:
                    if e.alert and "播放器" in str(e):
                        raise
                    log.warning("bilibili player cross-check unavailable: %s", e)
                raise Waiting("B 站暂无字幕")
            raise Waiting("B 站只有自动翻译字幕，没有原语言字幕")
        url = track.get("subtitle_url") or ""
        if not url:
            raise Pending("B 站字幕轨缺少下载地址", "bili", alert=True)
        url = "https:" + url if url.startswith("//") else url.replace("http://", "https://", 1)
        body = (_bili_json(client, url).get("body") or [])
    segments = [{"start": float(i["from"]), "end": float(i["to"]), "text": i["content"].strip()}
                for i in body if (i.get("content") or "").strip()]
    if not segments:
        raise Waiting("B 站字幕轨为空")
    lan = track["lan"]
    return {"platform": "bilibili", "video_id": ref["bvid"], "part_id": ref["cid"], "page": ref["page"],
            "language": lan[3:] if lan.startswith("ai-") else lan,
            "source_kind": "platform_auto" if lan.startswith("ai-") else "platform_manual",
            "track": {"lan": lan, "lan_doc": track.get("lan_doc", ""), "type": track.get("type"),
                      "ai_type": track.get("ai_type")},
            "available_tracks": [t["lan"] for t in tracks],
            "timestamps": "exact", "segments": segments}


# ---------- YouTube via Gemini ----------

_TS_LINE = re.compile(r"^\s*\[(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.\d+)?\]\s*(.*)$")
_NO_SPEECH = re.compile(r"^\(?\s*no speech\s*\)?$", re.I)
# Gemini sometimes returns a whole slice on one line: "[00:00] a [00:01] b …"
# (1 of 3 Karpathy slices, 10-15). Each stamp then starts a line of its own.
_INLINE_TS = re.compile(r"[ \t]+(?=\[(?:\d+:)?\d{1,2}:\d{2}(?:\.\d+)?\])")


def parse_lines(text: str, start: int, end: int) -> list[dict]:
    """[MM:SS] lines -> segments, made absolute if the model answered relative."""
    rows = []
    for raw in _INLINE_TS.sub("\n", text or "").splitlines():
        line = raw.strip()
        if not line or set(line) <= set("-=*`"):
            continue
        m = _TS_LINE.match(line)
        if m:
            h, mi, s, body = m.groups()
            rows.append([int(h or 0) * 3600 + int(mi) * 60 + int(s), body.strip()])
        elif rows:
            rows[-1][1] = (rows[-1][1] + " " + line).strip()
    if rows and start > 60:
        stamps = [r[0] for r in rows]
        if max(stamps) <= (end - start) + 60 and min(stamps) < start - 60:
            rows = [[t + start, body] for t, body in rows]
    return [{"start": float(t), "text": body} for t, body in rows]


def quota_day(now: float) -> str:
    """The Gemini free-tier quota day: the date in Pacific time."""
    return datetime.datetime.fromtimestamp(now, ZoneInfo(config.GEMINI_QUOTA_TZ)).date().isoformat()


def quota_reset(now: float) -> float:
    """Shortly after the next Pacific midnight, as an epoch time."""
    tz = ZoneInfo(config.GEMINI_QUOTA_TZ)
    day = datetime.datetime.fromtimestamp(now, tz).date() + datetime.timedelta(days=1)
    return datetime.datetime.combine(day, datetime.time(), tz).timestamp() + QUOTA_RESET_GRACE


def quota_spent(now: float) -> "Pending":
    at = quota_reset(now)
    tz = ZoneInfo(config.USER_TIMEZONE)
    when, today = datetime.datetime.fromtimestamp(at, tz), datetime.datetime.fromtimestamp(now, tz).date()
    day = {today: "今天", today + datetime.timedelta(days=1): "明天"}.get(when.date(), f"{when:%m-%d}")
    return Pending(f"今日 Gemini 免费额度已用完，{day} {when:%H:%M} 继续", "quota", delay=at - now)


def gemini_chunk(ref: dict, start: int, end: int, language: str = "") -> dict:
    lang = f" (expected language: {language})" if language else ""
    part = {"file_data": {"file_uri": ref["url"]},
            "video_metadata": {"start_offset": f"{start}s", "end_offset": f"{end}s"}}
    body = {"contents": [{"role": "user", "parts": [
                part, {"text": GEMINI_PROMPT.format(lang=lang, start=start, end=end)}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 65536,
                                 "mediaResolution": "MEDIA_RESOLUTION_LOW"}}
    now = time.time()
    if store.take_gemini_request(config.GEMINI_MODEL, quota_day(now), config.GEMINI_DAILY_REQUESTS) is None:
        raise quota_spent(now)
    began = time.monotonic()
    # One request per round: an immediate retry after a 503 would spend
    # another of the day's requests. Retries go through the schedule.
    try:
        r = httpx.post(f"{GEMINI_API}/models/{config.GEMINI_MODEL}:generateContent",
                       headers={"x-goog-api-key": config.GEMINI_API_KEY}, json=body,
                       timeout=work_budget.timeout(GEMINI_TIMEOUT, reserve=20))
    except httpx.HTTPError as e:
        raise Pending(f"Gemini 请求超时或网络异常：{type(e).__name__}", "gemini") from None
    if r.status_code != 200:
        try:
            err = r.json().get("error") or {}
        except ValueError:
            err = {}
        message = str(err.get("message") or r.text[:200])
        if r.status_code == 429:
            details = err.get("details") or []
            daily = any("PerDay" in (v.get("quotaId") or "") for d in details for v in d.get("violations") or [])
            if daily:
                # Its retryDelay pointed at UTC midnight while the day really
                # resets at Pacific midnight (10-16), so wait for that instead.
                now = time.time()
                store.spend_gemini_day(config.GEMINI_MODEL, quota_day(now), config.GEMINI_DAILY_REQUESTS)
                raise quota_spent(now)
            raise Pending("Gemini 免费层限流（429），稍后重试", "gemini")
        if r.status_code >= 500:
            raise Pending(f"Gemini 暂时不可用（{r.status_code}），稍后重试", "gemini")
        if r.status_code in (401, 403):
            raise GiveUp(f"Gemini 拒绝访问（{r.status_code}），请检查免费层密钥", alert=True)
        raise GiveUp(f"Gemini 无法读取该视频（{r.status_code}）：{message[:160]}")
    data = r.json()
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason") or "无候选结果"
        raise Pending(f"Gemini 未返回转录（{reason}）", "gemini")
    c = candidates[0]
    text = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts", []) if not p.get("thought"))
    return {"text": text, "finish": c.get("finishReason") or "", "usage": data.get("usageMetadata") or {},
            "model": config.GEMINI_MODEL, "elapsed_s": round(time.monotonic() - began, 1)}


def youtube_transcript(ref: dict, job: dict, say) -> dict:
    if not config.GEMINI_API_KEY:
        raise GiveUp("未配置 Gemini 免费层密钥，YouTube 视频暂不能转录", alert=True)
    duration = job["meta"]["duration"]
    if duration <= 0:
        raise GiveUp("YouTube 未提供视频时长，无法分段转录")
    if not job.get("plan"):
        checkpoint(job, plan=[[s, min(s + CHUNK_SECONDS, duration)] for s in range(0, duration, CHUNK_SECONDS)])
    done = dict(job.get("chunks") or {})
    while True:
        todo = [c for c in job["plan"] if f"{c[0]}-{c[1]}" not in done]
        if not todo:
            break
        start, end = todo[0]
        if work_budget.remaining() < GEMINI_TIMEOUT + 30:
            raise Pending("本轮时间用完，已转录片段已保存，自动继续")
        index = job["plan"].index([start, end]) + 1
        say(f"⏳ Gemini 正在转录 {_clock(start)}–{_clock(end)}（第 {index}/{len(job['plan'])} 段，每段约 3–6 分钟）…")
        result = gemini_chunk(ref, start, end, job["meta"].get("language", ""))
        if result["finish"] == "MAX_TOKENS":
            if end - start < MIN_CHUNK_SECONDS * 2:
                raise GiveUp(f"{_clock(start)}–{_clock(end)} 的转录超出单次输出上限")
            mid = (start + end) // 2
            plan = list(job["plan"])
            i = plan.index([start, end])
            plan[i:i + 1] = [[start, mid], [mid, end]]
            checkpoint(job, plan=plan)
            continue
        if result["finish"] not in ("STOP", ""):
            raise Pending(f"Gemini 转录未正常结束（{result['finish']}）", "gemini")
        segments = parse_lines(result["text"], start, end)
        if not segments:
            raise Pending(f"Gemini 对 {_clock(start)}–{_clock(end)} 没有返回转录行", "gemini")
        uri = put_json(job, f"chunk-{start}-{end}", {"start": start, "end": end, "raw": result["text"],
                                                     "finish": result["finish"], "usage": result["usage"],
                                                     "model": result["model"], "elapsed_s": result["elapsed_s"]})
        done[f"{start}-{end}"] = {"uri": uri, "first": segments[0]["start"], "last": segments[-1]["start"]}
        checkpoint(job, chunks=done, failure_streak=0, quota_waits=0)
    segments, raw_coverage = [], []
    for start, end in job["plan"]:
        info = done[f"{start}-{end}"]
        chunk = get_json(info["uri"])
        rows = parse_lines(chunk["raw"], start, end)
        for i, row in enumerate(rows):
            row["end"] = rows[i + 1]["start"] if i + 1 < len(rows) else float(end)
        raw_coverage.append({"start": start, "end": end, "first": info["first"], "last": info["last"]})
        segments.extend(r for r in rows if not _NO_SPEECH.match(r["text"]))
    if not segments:
        raise GiveUp("Gemini 转录全部为无语音段")
    return {"platform": "youtube", "video_id": ref["video_id"], "part_id": None,
            "language": job["meta"].get("language") or "", "source_kind": "model_transcript",
            "model": config.GEMINI_MODEL, "timestamps": "approximate",
            "chunks": raw_coverage, "segments": segments}


# ---------- durable artifacts ----------

def _encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode()


def put_json(job: dict, kind: str, value) -> str:
    data = _encoded(value)
    sha = hashlib.sha256(data).hexdigest()
    uri = storage.archive(data, f"video-{store.url_hash(job['key'])}-{kind}-{sha[:20]}.json",
                          "application/json; charset=utf-8")
    if not uri:
        raise Pending("视频中间结果归档失败，已完成进度保留", "llm")
    return uri


def get_json(uri: str):
    data = storage.read_archive(uri, limit=32 * 1024 * 1024)
    expected = re.search(r"-([a-f0-9]{20})\.json$", uri)
    if expected is None or hashlib.sha256(data).hexdigest()[:20] != expected.group(1):
        raise Pending("视频检查点 SHA 校验失败，停止使用该检查点", "llm")
    return json.loads(data)


def checkpoint(job: dict, **fields) -> None:
    store.update_video_job(job["key"], **fields)
    job.update(fields)


# ---------- text shaping ----------

def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def plain_text(transcript: dict) -> str:
    return "\n".join(f"[{_clock(s['start'])}] {s['text']}" for s in transcript["segments"])


def sections(segments: list[dict], limit: int = SECTION_CHARS) -> list[dict]:
    """Consecutive time ranges of at most ~limit transcript characters."""
    out, current, size = [], [], 0
    for seg in segments:
        n = len(seg["text"]) + len(_clock(seg["start"])) + 4   # as rendered: "[MM:SS] text\n"
        if current and size + n > limit:
            out.append(current)
            current, size = [], 0
        current.append(seg)
        size += n
    if current:
        out.append(current)
    return [{"start": c[0]["start"], "end": c[-1].get("end") or c[-1]["start"],
             "text": "\n".join(f"[{_clock(s['start'])}] {s['text']}" for s in c), "segments": c}
            for c in out]


def paragraphs_for(segments: list[dict], joiner: str) -> list[str]:
    """Group lines into roughly one paragraph per minute, each led by its time."""
    out, block, began = [], [], None
    for seg in segments:
        if began is None:
            began = seg["start"]
        block.append(seg["text"])
        if seg["start"] - began >= PARAGRAPH_SECONDS:
            out.append(f"[{_clock(began)}] " + joiner.join(block))
            block, began = [], None
    if block:
        out.append(f"[{_clock(began)}] " + joiner.join(block))
    return out


def _joiner(language: str) -> str:
    return "" if (language or "").lower().startswith(("zh", "ja", "ko")) else " "


# ---------- summary ----------

def summarize_section(title: str, part: dict, index: int, total: int, source_kind: str) -> dict:
    prompt = (f"视频标题：{title}\n第 {index}/{total} 段，时间 {_clock(part['start'])}–{_clock(part['end'])}\n"
              f"文本来源：{SOURCE_LABEL.get(source_kind, source_kind)}\n\n转录：\n{part['text']}")
    messages = [{"role": "system", "content": SECTION_SYSTEM}, {"role": "user", "content": prompt}]
    content = None
    for use_schema in ((True, False) if config.LLM_USE_SCHEMA else (False,)):
        try:
            content = enrich._chat(messages, use_schema, max_tokens=3000, json_schema=SECTION_SCHEMA,
                                   request_timeout=config.IMAGE_LLM_TIMEOUT, disable_thinking=True)
            break
        except enrich._Rejected as e:
            if e.status >= 500 or e.status == 200 or not use_schema:
                raise Pending(f"分段整理请求未完成：{str(e)[:160]}", "llm") from None
        except httpx.HTTPError as e:
            raise Pending(f"分段整理请求超时或网络异常：{type(e).__name__}", "llm") from None
    try:
        data = enrich._extract_json(content)
        points = [str(p).strip() for p in data.get("points") or [] if str(p).strip()]
        if not str(data.get("title") or "").strip() or not points:
            raise ValueError("小标题或要点为空")
    except (ValueError, TypeError, AttributeError) as e:
        raise Pending(f"分段整理结果校验失败：{e}", "llm") from None
    return {"title": str(data["title"]).strip()[:80], "points": points[:8]}


def summarize(job: dict, transcript: dict, note: str, say) -> enrich.Enrichment:
    note = ""  # Link-only policy also applies to checkpoints from older jobs.
    if job.get("summary_uri"):
        return enrich.Enrichment.model_validate(get_json(job["summary_uri"]))
    meta, title = job["meta"], job["meta"]["title"] or "未命名视频"
    parts = sections(transcript["segments"])
    full = plain_text(transcript)
    hint = (enrich.VIDEO_SUMMARY_HINT + f"{job['ref']['platform']} 视频「{title}」，作者 {meta.get('author') or '未知'}；"
            f"文本来源：{SOURCE_LABEL[transcript['source_kind']]}。")
    if len(parts) == 1:
        body = full
        hint += "以下是完整转录。"
    else:
        done = dict(job.get("section_steps") or {})
        for i, part in enumerate(parts, 1):
            key = f"{part['start']:.0f}-{part['end']:.0f}"
            if key in done:
                continue
            if work_budget.remaining() < config.IMAGE_LLM_TIMEOUT + 65:
                raise Pending("分段整理进度已保存，下轮继续")
            say(f"⏳ 正在分段整理 {i}/{len(parts)}（{_clock(part['start'])}–{_clock(part['end'])}）…")
            value = summarize_section(title, part, i, len(parts), transcript["source_kind"])
            done[key] = {"uri": put_json(job, f"section-{key}", value), "start": part["start"], "end": part["end"]}
            checkpoint(job, section_steps=done, failure_streak=0)
        chapters = []
        for part in parts:
            step = done[f"{part['start']:.0f}-{part['end']:.0f}"]
            chapters.append({**get_json(step["uri"]), "start": part["start"], "end": part["end"]})
        checkpoint(job, chapters_uri=put_json(job, "chapters", chapters))
        body = "\n\n".join(f"[{_clock(c['start'])}–{_clock(c['end'])}] {c['title']}\n" +
                           "\n".join(f"- {p}" for p in c["points"]) for c in chapters)
        if len(body) > DIGEST_LIMIT:
            raise GiveUp(f"章节要点共 {len(body)} 字，超过整体汇总容量；转录已归档，需人工处理")
        hint += f"以下是完整转录按时间切成 {len(parts)} 段后逐段整理的全部章节要点。"
    if work_budget.remaining() < config.IMAGE_LLM_TIMEOUT + 65:
        raise Pending("分段整理已完成，整体总结将在下一轮继续")
    say("⏳ 正在汇总整段视频的中文概览和要点…")
    try:
        en, error = enrich.enrich(title, body, hint + job["ref"]["url"], note=note,
                                  request_timeout=config.IMAGE_LLM_TIMEOUT, disable_thinking=True,
                                  evidence_text=full + "\n" + body)
    except enrich.EndpointUnavailable as e:
        raise Pending(f"LLM 请求未完成：{str(e)[:160]}", "llm") from None
    if error:
        raise Pending(error, "llm")
    en.material_type, en.fallback_category = "视频", "video"
    checkpoint(job, summary_uri=put_json(job, "summary", en.model_dump()))
    return en


# ---------- Notion page ----------

def _link_heading(text: str, url: str, level: int = 3) -> dict:
    kind = f"heading_{level}"
    return {"object": "block", "type": kind,
            kind: {"rich_text": [{"type": "text", "text": {"content": text[:1900], "link": {"url": url}}}]}}


def _link_paragraph(prefix: str, label: str, url: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": prefix}},
        {"type": "text", "text": {"content": label, "link": {"url": url}}}]}}


def coverage_lines(job: dict, transcript: dict) -> list[str]:
    meta, segs = job["meta"], transcript["segments"]
    lines = [f"文本来源：{SOURCE_LABEL[transcript['source_kind']]}"]
    if transcript.get("track"):
        t = transcript["track"]
        lines.append(f"字幕轨：{t.get('lan_doc') or t['lan']}（{t['lan']}）；该视频全部字幕轨："
                     + "、".join(transcript.get("available_tracks") or []))
    if transcript.get("model"):
        lines.append(f"转录模型：{transcript['model']}（免费层），按 {CHUNK_SECONDS // 60} 分钟分 "
                     f"{len(transcript.get('chunks') or [])} 段请求后拼接")
    span = f"覆盖 {_clock(segs[0]['start'])}–{_clock(segs[-1].get('end') or segs[-1]['start'])}"
    if meta.get("duration"):
        span += f"，视频时长 {_clock(meta['duration'])}"
        last = max(s.get("end") or s["start"] for s in segs)
        if meta["duration"] - last > 120:
            span += f"；末尾约 {_clock(meta['duration'] - last)} 没有转录文本（可能为无语音段）"
    lines.append(span + f"；共 {len(segs)} 条、{sum(len(s['text']) for s in segs)} 字")
    if transcript.get("timestamps") == "approximate":
        lines.append("时间点为模型给出的近似值（实测中位偏差约 1–4 秒，个别达 15 秒），只作大致定位")
    lines.append("页面基于字幕/语音文本整理，未读取画面中的 PPT、代码、图表或公式")
    return lines


def page_blocks(job: dict, transcript: dict, en: enrich.Enrichment, chapters: list[dict] | None,
                sources: list[str]) -> list[dict]:
    from . import notion, pipeline
    ref, meta = job["ref"], job["meta"]
    info = [x for x in (f"{'频道' if ref['platform'] == 'YouTube' else 'UP 主'}：{meta['author']}" if meta.get("author") else "",
                        f"发布：{meta['published']}" if meta.get("published") else "",
                        f"时长：{_clock(meta['duration'])}" if meta.get("duration") else "") if x]
    blocks = [_link_paragraph(" · ".join(info) + (" · " if info else ""), "观看原视频", ref["url"])]
    blocks += [notion.heading("中文概览"), *notion.paragraphs(en.overview)]
    if en.key_points:
        blocks += [notion.heading("中文要点"), *notion.bullets(en.key_points)]
    joiner = _joiner(transcript.get("language", ""))
    parts = sections(transcript["segments"])
    if chapters:
        blocks.append(notion.heading("章节要点"))
        for c in chapters:
            blocks.append(_link_heading(f"{_clock(c['start'])}–{_clock(c['end'])} {c['title']}", jump_url(ref, c["start"])))
            blocks.extend(notion.bullets(c["points"]))
    blocks.append(notion.heading("完整转录"))
    for i, part in enumerate(parts):
        label = f"{_clock(part['start'])}–{_clock(part['end'])}"
        if chapters:
            label += " " + chapters[i]["title"]
        blocks.extend(pipeline._folded_blocks(label, notion.paragraphs("\n".join(paragraphs_for(part["segments"], joiner)))))
    blocks += [notion.heading("转录来源与范围"),
               notion.callout("机器整理：中文概览与要点由 LLM 根据下列文本生成；"
                              + ("文本为模型转录，可能有遗漏或误听。" if transcript["source_kind"] == "model_transcript"
                                 else "文本为平台字幕，自动字幕可能有误听。"), "🤖"),
               *notion.bullets(coverage_lines(job, transcript))]
    blocks.append(notion.toggle("来源与定位", notion.bullets(sources)))
    return blocks


def publish(job: dict, transcript: dict, en: enrich.Enrichment, say) -> tuple[str, str]:
    from . import chat_notes, notion, pipeline, telegram
    ref, meta, msg = job["ref"], job["meta"], job["msg"]
    title = meta["title"] or en.title
    chapters = get_json(job["chapters_uri"]) if job.get("chapters_uri") else None
    sources = [f"原始链接：{ref['url']}"]
    if job.get("source_url") and job["source_url"] != ref["url"]:
        sources.append(f"分享链接：{job['source_url']}")
    if ref.get("start"):
        sources.append(f"分享时的跳转起点：{_clock(ref['start'])}")
    sources += [f"收集时间：{job['imported_at']}", f"转录归档（JSON）：{job['transcript_uri']}",
                f"转录归档（纯文本）：{job['text_uri']}", f"视频实体：{ref['key']}"]
    blocks = page_blocks(job, transcript, en, chapters, sources)
    page = job.get("page")
    if not page:
        checkpoint(job, writing_started=True)
        # Adopt a page whose create succeeded but whose response was lost.
        matches = notion._call("POST", f"/databases/{config.NOTION_MATERIAL_DB}/query",
                               {"filter": {"property": "原始链接", "url": {"equals": ref["url"]}}})["results"]
        matches = [m for m in matches if not (m.get("archived") or m.get("in_trash"))]
        if len(matches) > 1:
            raise GiveUp("同一视频链接已有多份资料页，停止自动写入")
        if matches and notion.prop_text(matches[0], "处理状态") not in ("需处理", "等待采集"):
            raise GiveUp("发现未登记的已完成视频页面，需核对后继续")
        if matches:
            page = {"id": matches[0]["id"], "url": matches[0]["url"]}
        else:
            say("⏳ 正在写入 Notion…")
            created = notion.create_material(title=title, material_type="视频", platform=ref["platform"],
                                             url=ref["url"], topics=en.topics, processing="需处理", blocks=[])
            page = {"id": created["id"], "url": created["url"]}
        checkpoint(job, page=page)
    if not job.get("body_verified"):
        blocks.append(notion.heading(pipeline.NOTE_SECTION))
        link = telegram.note_deeplink(page["id"])
        if link:
            blocks.append(notion.link_callout(pipeline.NOTE_ENTRY_PREFIX, pipeline.NOTE_ENTRY_LABEL, link))
        try:
            chat_notes._append_prefix(page["id"], blocks)
        except chat_notes.chat_share.UnreadableShare as e:
            raise GiveUp(str(e)) from None
        current = notion.get_page(page["id"])
        notion.refresh_machine_fields(page["id"], title=title, material_type="视频", topics=en.topics,
                                      overview=en.overview, processing="完成", existing_page=current)
        checkpoint(job, body_verified=True)
    if msg.get("message_id"):
        rid = store.put_record(msg["chat"]["id"], msg["message_id"], kind="material",
                               notion_page_id=page["id"], notion_url=page["url"], url=ref["url"])
        for alias in {ref["key"], ref["url"], job.get("source_url") or ref["url"]}:
            store.index_url(alias, rid)
        pipeline.explicit_relations.bind_material(msg, page["id"], ref["url"])
    # Ignore attached text from historical jobs as well.
    pipeline._claim_staged(ref["url"], page["id"])
    checkpoint(job, status="done", stage="done", error="", finished_at=time.time())
    if job.get("update_id") is not None:
        store.set_update_status(job["update_id"], "done")
    try:
        art = pipeline.thumbnails.material_art("视频", ref["platform"], ref["url"], fallback_category="video",
                                               candidates={"shares": [meta["thumbnail"]] if meta.get("thumbnail") else [],
                                                           "body": []})
        if art and work_budget.remaining() > 20:
            with work_budget.limit(min(10, work_budget.remaining() - 15)):
                notion.decorate_page(page["id"], **art)
    except Exception:
        log.info("video decoration skipped; material verified")
    return pipeline._success_text("✅ 已整理", title, en.overview, source_url=ref["url"],
                                  platform=ref["platform"], tag_review=en.tag_review), page["url"]


# ---------- job driver ----------

def _schedule(job: dict, delay: float) -> bool:
    number = job.get("round", 0) + 1
    checkpoint(job, round=number, next_retry_at=time.time() + max(delay, 1))
    return tasks.enqueue_video(job["key"], number, int(max(delay, 1)))


def _when(seconds: float) -> str:
    if seconds < 90:
        return "约 1 分钟后"
    if seconds < 5400:
        return f"约 {round(seconds / 60)} 分钟后"
    return f"约 {round(seconds / 3600)} 小时后"


def _stage(job: dict, reason: str, what: str) -> str:
    from . import pipeline
    checkpoint(job, status="staged", error=reason, next_retry_at=0)
    msg = job["msg"]
    text, _ = pipeline._stage(msg, job["ref"]["url"], reason, job.get("note") or "", what,
                              material_title=(job.get("meta") or {}).get("title", ""))
    return text


def _alert(job: dict, reason: str) -> None:
    # A distinct marker for log-based alerting; the reply says it too.
    log.error("VIDEO_ALERT key=%s platform=%s reason=%s", job["key"], job["ref"]["platform"], reason)
    checkpoint(job, alert=reason, alert_at=time.time())


def advance(job: dict, say) -> tuple[str, str]:
    ref = job["ref"]
    if not job.get("transcript_uri"):
        if ref["platform"] == "YouTube":
            if not job.get("meta"):
                checkpoint(job, meta=youtube_meta(ref))
            transcript = youtube_transcript(ref, job, say)
        else:
            say("⏳ 正在取 B 站字幕…")
            transcript = bilibili_transcript(ref, job)
            checkpoint(job, meta=job["meta"])
        transcript.update(fetched_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          title=job["meta"]["title"], url=ref["url"], version=VERSION)
        text = plain_text(transcript)
        text_uri = storage.archive_text(text, f"video-{store.url_hash(job['key'])}-transcript.txt")
        if not text_uri:
            raise Pending("转录全文归档失败，稍后重试", "llm")
        checkpoint(job, transcript_uri=put_json(job, "transcript", transcript), text_uri=text_uri,
                   stage="summary", failure_streak=0, wait_count=0,
                   transcript_chars=len(text), source_kind=transcript["source_kind"])
        if job.get("handoff"):
            # The first round runs on the message queue; free it for new messages.
            checkpoint(job, handoff=False)
            raise Pending(f"{'字幕' if ref['platform'] == 'B站' else '转录'}已取得（{len(text)} 字），正在后台分段整理")
    transcript = get_json(job["transcript_uri"])
    en = summarize(job, transcript, job.get("note") or "", say)
    if work_budget.remaining() < 30:
        raise Pending("整理已保存，下轮写入 Notion")
    checkpoint(job, stage="publish")
    return publish(job, transcript, en, say)


def run(job: dict, say, *, manual: bool) -> tuple[str, str]:
    """One round: advance as far as the budget allows, then decide what is next."""
    from . import telegram
    platform = job["ref"]["platform"]
    try:
        checkpoint(job, status="processing")
        return advance(job, say)
    except Waiting as e:
        count = job.get("wait_count", 0)
        if manual and job.get("status_before") == "staged" or count >= len(BILI_WAIT_DELAYS):
            waited = sum(BILI_WAIT_DELAYS[:count])
            reason = f"{e}" + (f"（已等待约 {round(waited / 3600)} 小时）" if waited else "")
            return _stage(job, reason, "B 站字幕"), ""
        if manual and job.get("wait_count"):
            checkpoint(job, status="waiting")
            return (f"⏳ {telegram.esc(str(e))}，仍在等 B 站生成 AI 字幕；"
                    f"下次自动检查{_when(max(0, job.get('next_retry_at', 0) - time.time()))}。"), ""
        delay = BILI_WAIT_DELAYS[count]
        checkpoint(job, status="waiting", wait_count=count + 1, error=str(e))
        _schedule(job, delay)
        return (f"⏳ {telegram.esc(str(e))}。新视频的 AI 字幕常会延迟生成，"
                f"{_when(delay)}自动再查（共 {len(BILI_WAIT_DELAYS)} 次）；仍没有就放进暂存区。"), ""
    except GiveUp as e:
        if e.alert:
            _alert(job, str(e))
        text = _stage(job, str(e), "视频文本" if platform == "YouTube" else "B 站字幕")
        return text + ("\n⚠️ 已记录告警。" if e.alert else ""), ""
    except (Pending, work_budget.Exhausted) as e:
        kind = getattr(e, "kind", None)
        message = str(e)
        if kind == "quota":
            waits = job.get("quota_waits", 0)
            if waits >= QUOTA_WAITS or manual and job.get("status_before") == "staged":
                return _stage(job, message, "视频转录"), ""
            checkpoint(job, status="pending", quota_waits=waits + 1, error=message)
            if not _schedule(job, e.delay):
                return _stage(job, message, "视频转录"), ""
            return f"⏳ {telegram.esc(message)}，已转录片段已保存。", ""
        if kind is None:
            # Budget ran out with progress saved: continue at once, not a failure.
            checkpoint(job, status="pending", error=message)
            queued = _schedule(job, 1)
            if not queued:
                return _stage(job, message, "视频整理"), ""
            return f"⏳ {telegram.esc(message)}，完成后会更新这条消息。", ""
        delays = {"bili": BILI_ERROR_DELAYS, "gemini": GEMINI_DELAYS}.get(kind, LLM_DELAYS)
        streak = job.get("failure_streak", 0)
        if streak >= len(delays) or manual and job.get("status_before") == "staged":
            if getattr(e, "alert", False):
                _alert(job, message)
            text = _stage(job, message, "B 站字幕" if kind == "bili" else "视频转录" if kind == "gemini" else "视频整理")
            return text + ("\n⚠️ B 站字幕接口异常，不按无字幕处理，已记录告警。" if getattr(e, "alert", False) else ""), ""
        checkpoint(job, status="pending", failure_streak=streak + 1, error=message)
        queued = _schedule(job, delays[streak])
        if not queued:
            return _stage(job, message, "视频整理"), ""
        return f"⏳ {telegram.esc(message)}。{_when(delays[streak])}自动重试，进度已保存。", ""
    except Exception as e:
        crashes = job.get("crashes", 0) + 1
        error = telegram.redact(str(e))[:500]
        if crashes >= 3:
            # The queue and drain would otherwise retry an unknown error forever.
            log.exception("video job %s failed %s times; staging", job["key"], crashes)
            checkpoint(job, crashes=0)
            return _stage(job, f"处理出错：{error[:160]}", "视频整理"), ""
        checkpoint(job, status="pending", error=error, crashes=crashes)
        raise


def _fresh_job(job: dict, ref: dict, msg: dict, note: str, update_id, url: str) -> None:
    from . import pipeline
    checkpoint(job, ref=ref, msg=msg, note=note, update_id=update_id,
               source_url=url, status="processing", stage="transcript", version=VERSION,
               imported_at=pipeline._now(), started_at=time.time(), wait_count=0, failure_streak=0,
               round=job.get("round", 0))


def _queue_available() -> bool:
    return bool(config.GCP_PROJECT and config.SERVICE_URL and config.TASKS_SA_EMAIL)


def handle(msg: dict, url: str, ref: dict, note: str, say, update_id=None) -> tuple[str, str]:
    """Ingest a video link or resume it; attached text is ignored."""
    note = ""  # Ignore text accompanying this link.
    owner = store.claim_video_job(ref["key"])
    if not owner:
        return "⏳ 这条视频正在处理，进度会保留。", ""
    job = store.get_video_job(ref["key"]) or {}
    job["key"] = ref["key"]
    try:
        status = job.get("status")
        if status == "done":
            from . import notion
            if job.get("page") and notion.page_alive(job["page"]["id"]):
                return "♻️ 已保存过", job["page"]["url"]
            # The page was deleted in Notion: import afresh, keep the old job.
            history = put_json(job, "previous-job", {k: v for k, v in job.items() if k != "lease_owner"})
            checkpoint(job, previous_job_uri=history, page=None, body_verified=False, writing_started=False,
                       summary_uri=None, section_steps={}, chapters_uri=None, transcript_uri=None,
                       text_uri=None, plan=None, chunks={}, meta=None)
            status = None
        if not status:
            _fresh_job(job, ref, msg, note, update_id, url)
            if _queue_available():
                if ref["platform"] == "YouTube" and _schedule(job, 1):
                    return "⏳ 已开始 YouTube 转录（Gemini 免费层，按 15 分钟分段），完成后会更新这条消息。", ""
                # B站 subtitles take seconds: fetch now so waiting is reported at
                # once, then summarize on the video queue.
                checkpoint(job, handoff=True)
            return run(job, say, manual=False)
        # Resend or /retry of an unfinished job: keep checkpoints, check now.
        checkpoint(job, status_before=status,
                   failure_streak=0 if status == "staged" else job.get("failure_streak", 0))
        return run(job, say, manual=True)
    finally:
        store.release_video_job(ref["key"], owner)


def retry_page(url: str, say) -> tuple[bool, str]:
    """A 需处理 video page from /retry: only a video job can finish it."""
    from . import telegram
    try:
        ref = identify(url)
    except (GiveUp, Pending) as e:
        return False, telegram.esc(str(e))
    job = store.get_video_job(ref["key"])
    if not job or not job.get("msg"):
        return False, telegram.esc(f"{url}：没有对应的视频任务，请重新发送视频链接")
    text, link = handle(job["msg"], url, ref, "", say, job.get("update_id"))
    return bool(link), text


def resume(key: str, *, notify: bool = True) -> dict:
    from . import telegram
    job = store.get_video_job(key)
    if not job or job.get("status") in ("done", "staged"):
        return {"ok": True, "skipped": True}
    if job.get("lease_expires", 0) > time.time():
        return {"ok": False, "busy": True}
    if job.get("next_retry_at", 0) > time.time() + 60:
        # A newer schedule superseded this task (e.g. after a manual check).
        return {"ok": True, "skipped": True, "next_retry_at": job["next_retry_at"]}
    owner = store.claim_video_job(key)
    if not owner:
        return {"ok": False, "busy": True}
    msg = job["msg"]
    update = store.get_update(job["update_id"]) if job.get("update_id") is not None else {}
    ack = (update or {}).get("ack_message_id")
    last = [0.0]

    def say(text):
        if notify and ack and time.monotonic() - last[0] > 25:
            last[0] = time.monotonic()
            try:
                telegram.edit(msg["chat"]["id"], ack, text)
            except Exception:
                pass
    try:
        job = store.get_video_job(key) or job
        job["key"] = key
        before = job.get("status")
        checkpoint(job, status_before="")
        with work_budget.limit(ROUND_SECONDS):
            text, notion_url = run(job, say, manual=False)
    finally:
        store.release_video_job(key, owner)
    final = job.get("status") in ("done", "staged")
    changed = job.get("status") != before or final
    if notify and msg.get("chat") and changed:
        # Hours of waiting end in a new message; an edit would go unnoticed.
        long_wait = time.time() - job.get("started_at", time.time()) > 900
        try:
            if ack and not (final and long_wait):
                telegram.edit(msg["chat"]["id"], ack, text, notion_url=notion_url or None, parse_mode="HTML")
            elif final:
                telegram.send(msg["chat"]["id"], text, reply_to=msg.get("message_id"),
                              notion_url=notion_url or None, parse_mode="HTML")
        except Exception:
            log.warning("video result notification failed; state is saved")
    return {"ok": True, "completed": bool(notion_url), "status": job.get("status")}


def drain() -> int:
    """Re-dispatch due jobs whose continuation task was lost."""
    sent, now = 0, time.time()
    for job in store.pending_video_jobs():
        # Overdue by ten minutes and untouched since: the scheduled task is gone.
        if (not job.get("ref") or job.get("lease_expires", 0) > now
                or job.get("next_retry_at", 0) + 600 > now or job.get("updated_at", 0) + 600 > now):
            continue
        sent += bool(_schedule(job, 1))
    return sent


def fetch_text(url: str) -> dict:
    """fetch_for()'s video answer: the archived transcript once one exists.

    Video links do not come through the generic fetch path -- they run as
    durable jobs -- so this only serves callers that need text for a video
    already transcribed, and says so plainly otherwise.
    """
    try:
        ref = identify(url)
    except (GiveUp, Pending, Waiting) as e:
        return {"ok": False, "title": "", "text": "", "error": str(e)}
    job = store.get_video_job(ref["key"]) or {}
    if job.get("transcript_uri"):
        transcript = get_json(job["transcript_uri"])
        return {"ok": True, "title": (job.get("meta") or {}).get("title", ""),
                "text": plain_text(transcript), "error": "", "video_ref": ref}
    return {"ok": False, "title": "", "text": "", "video_ref": ref,
            "error": "视频文本由后台视频任务获取，请重新发送链接或 /retry"}
