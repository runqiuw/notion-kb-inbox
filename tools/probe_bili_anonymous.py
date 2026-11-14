"""Anonymous Bilibili subtitle probe: no login cookie, no credentials, no Notion/model calls.

Chain: BV -> aid (local) + x/player/pagelist (cid) -> x/v2/dm/view (subtitle list)
-> subtitle JSON. Also records whether x/web-interface/view (title metadata) is reachable.
Prints one JSON line per sample; only needs httpx (already in the service image).
"""
import hashlib
import json
import os
import time

import httpx

SAMPLES = [
    ("bili_ml_p1", "BV1DTxXzUEvW", 1, "zh"),
    ("bili_ml_p2", "BV1DTxXzUEvW", 2, "zh"),
    ("bili_ml_2025", "BV1TAtwzTE1S", 1, "zh"),
    ("bili_op_p2", "BV1bK411W797", 2, "ja"),
    ("bili_talk", "BV13x41117TL", 1, "zh"),
    ("bili_music", "BV1GJ411x7h7", 1, "en"),
]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
ALPHABET = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"


def bv_to_aid(bvid):
    b = list(bvid)
    b[3], b[9] = b[9], b[3]
    b[4], b[7] = b[7], b[4]
    n = 0
    for c in b[3:]:
        n = n * 58 + ALPHABET.index(c)
    return (n & 2251799813685247) ^ 23442827791579


def get_json(client, url, **params):
    # httpx replaces an existing query string when params is passed, even empty;
    # the subtitle URL's signed auth_key lives in that query string.
    r = client.get(url, params=params) if params else client.get(url)
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        return r.status_code, None
    return r.status_code, r.json()


def probe(client, name, bvid, page, language):
    out = {"sample": name, "bvid": bvid, "page": page, "aid": bv_to_aid(bvid)}
    status, view = get_json(client, "https://api.bilibili.com/x/web-interface/view", bvid=bvid)
    out["view_http"] = status
    out["view_code"] = view.get("code") if view else None
    if view and not view.get("code"):
        out["title"] = view["data"]["title"]
    status, pages = get_json(client, "https://api.bilibili.com/x/player/pagelist", bvid=bvid)
    out["pagelist_http"] = status
    if not pages or pages.get("code") or not pages.get("data"):
        return {**out, "status": "pagelist_failed", "pagelist_code": pages.get("code") if pages else None}
    part = pages["data"][page - 1]
    out.update(cid=part["cid"], part_title=part["part"], duration=part["duration"])
    status, dm = get_json(client, "https://api.bilibili.com/x/v2/dm/view", type=1, oid=part["cid"], pid=out["aid"])
    out["dm_view_http"] = status
    if not dm or dm.get("code"):
        return {**out, "status": "dm_view_failed", "dm_view_code": dm.get("code") if dm else None}
    tracks = ((dm.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    out["tracks"] = [t["lan"] for t in tracks]
    if not tracks:
        return {**out, "status": "no_tracks_reported"}
    matching = sorted((t for t in tracks if language in t["lan"]), key=lambda t: t["lan"].startswith("ai-"))
    if not matching:
        return {**out, "status": "requested_language_missing"}
    track = matching[0]
    url = track["subtitle_url"]
    url = "https:" + url if url.startswith("//") else url.replace("http://", "https://", 1)
    status, sub = get_json(client, url)
    out.update(subtitle_http=status, language=track["lan"],
               source_kind="platform_auto" if track["lan"].startswith("ai-") else "platform_manual")
    if sub is None:
        return {**out, "status": "subtitle_fetch_failed"}
    body = sub.get("body") or []
    text = "\n".join(i["content"] for i in body)
    return {**out, "status": "ok" if body else "empty_subtitle",
            "segment_count": len(body), "text_chars": len(text),
            "last_end": body[-1]["to"] if body else None,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest()}


def main():
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    with httpx.Client(headers=headers, timeout=12, follow_redirects=True) as client:
        home = client.get("https://www.bilibili.com/")
        print(json.dumps({"event": "home", "http": home.status_code, "scope": os.environ.get("PROBE_SCOPE", "local"),
                          "cookie_names": sorted(client.cookies.keys())}), flush=True)
        for sample in SAMPLES:
            start = time.monotonic()
            try:
                result = probe(client, *sample)
            except Exception as exc:  # keep only the type: messages may carry signed URLs
                result = {"sample": sample[0], "status": "error", "error_type": type(exc).__name__}
            result["elapsed_s"] = round(time.monotonic() - start, 3)
            result["scope"] = os.environ.get("PROBE_SCOPE", "local")
            print(json.dumps(result, ensure_ascii=False), flush=True)
            time.sleep(1.5)


if __name__ == "__main__":
    main()
