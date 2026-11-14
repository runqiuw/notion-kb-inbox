"""Strict readers for public, text-only ChatGPT/Gemini shared conversations.

These are frontend formats, not stable APIs. Unknown shapes fail closed;
webpage chrome and a partially decoded conversation are never substitutes.
"""
import json
import re
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) Gecko/20100101 Firefox/135.0"
STREAM = re.compile(r'streamController\.enqueue\((".*?")\);\s*</script>', re.S)


class UnreadableShare(ValueError):
    pass


@dataclass
class Conversation:
    provider: str
    url: str
    title: str
    messages: list[dict]
    raw: str = ""

    def snapshot(self):
        return {k: v for k, v in asdict(self).items() if k != "raw"}


def is_share(url: str) -> bool:
    p = urlsplit(url)
    return ((p.hostname or "").lower() in ("chatgpt.com", "chat.openai.com", "gemini.google.com") and p.path.startswith("/share/")) or \
        ((p.hostname or "").lower() == "g.co" and p.path.startswith("/gemini/share/")) or \
        (p.hostname or "").lower() == "share.gemini.google"


def identify(url: str) -> tuple[str, str] | None:
    p = urlsplit(url)
    host, path = (p.hostname or "").lower(), p.path.rstrip("/")
    if host in ("chatgpt.com", "chat.openai.com"):
        m = re.fullmatch(r"/share/([0-9a-fA-F-]{36})", path)
        if m:
            return "ChatGPT", "https://chatgpt.com/share/" + m[1].lower()
    if host in ("gemini.google.com", "g.co"):
        pattern = r"/share/([0-9a-fA-F]{6,64})" if host == "gemini.google.com" else r"/gemini/share/([0-9a-fA-F]{6,64})"
        m = re.fullmatch(pattern, path)
        if m:
            return "Gemini", "https://gemini.google.com/share/" + m[1].lower()
    if host == "share.gemini.google" and re.fullmatch(r"/[A-Za-z0-9_-]+", path):
        return "Gemini", "https://share.gemini.google" + path
    return None


def resolve(url: str) -> str:
    ref = identify(url)
    if not ref:
        raise UnreadableShare("不是支持的公开会话分享链接")
    canonical = ref[1]
    if urlsplit(canonical).hostname == "share.gemini.google":
        r = httpx.get(canonical, headers={"User-Agent": UA}, follow_redirects=True, timeout=25)
        r.raise_for_status()
        final = identify(str(r.url))
        if not final or urlsplit(final[1]).hostname != "gemini.google.com":
            raise UnreadableShare("Gemini 短链接没有指向公开分享页")
        canonical = final[1]
    return canonical


def _find(value, key):
    if isinstance(value, dict):
        if key in value:
            return value[key]
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        result = _find(child, key)
        if result is not None:
            return result
    return None


def _hydrate(pool, index, active=None, depth=0):
    # Negative turbo-stream sentinels represent null/undefined. Typed objects
    # remain explicit markers so a message containing them cannot lose data.
    if type(index) is not int:
        raise UnreadableShare("ChatGPT 数据引用不是整数")
    if index < 0:
        return None
    active = set() if active is None else active
    if index >= len(pool) or index in active or depth > 100:
        raise UnreadableShare("ChatGPT 数据引用缺失或循环")
    value = pool[index]
    active.add(index)
    try:
        if isinstance(value, dict):
            out = {}
            for key, ref in value.items():
                if not re.fullmatch(r"_\d+", key) or int(key[1:]) >= len(pool):
                    raise UnreadableShare("ChatGPT 数据键不完整")
                name = pool[int(key[1:])]
                if not isinstance(name, str):
                    raise UnreadableShare("ChatGPT 数据键不是文字")
                out[name] = _hydrate(pool, ref, active, depth + 1)
            return out
        if isinstance(value, list):
            if value and isinstance(value[0], str):
                return {"_turbo_type": value[0]}
            return [_hydrate(pool, ref, active, depth + 1) for ref in value]
        return value
    finally:
        active.remove(index)


def _text_parts(parts):
    if not isinstance(parts, list) or not parts or any(not isinstance(p, str) for p in parts):
        raise UnreadableShare("消息含无法取得的图片、附件或非文字内容")
    text = "\n\n".join(parts)
    if not text.strip():
        raise UnreadableShare("存在空白或缺失的发言，无法确认会话完整")
    return text


def _validate(messages):
    if not messages or {m["role"] for m in messages} != {"user", "assistant"}:
        raise UnreadableShare("未取得同时包含用户和 AI 发言的完整对话")
    for i, message in enumerate(messages, 1):
        message["id"] = i
    return messages


def _timestamp(seconds):
    """Source epoch seconds only: never guess from text, IDs or import time."""
    if type(seconds) not in (int, float):
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _message_time(message):
    value = message.get("create_time")
    timestamp = _timestamp(value)
    return {"created_at": timestamp, "time_source": "message.create_time",
            "source_create_time": value} if timestamp else {}


def _turn_time(turn):
    # Observed Gemini RPC field is a TURN timestamp, not independent user/AI
    # message times. Keep its original seconds/nanoseconds and label that scope.
    value = _at(turn, 4)
    if (not isinstance(value, list) or len(value) != 2
            or any(type(v) is not int for v in value) or not 0 <= value[1] < 1000000000):
        return {}
    timestamp = _timestamp(value[0])
    if not timestamp:
        return {}
    timestamp = timestamp.split("+")[0] + f".{value[1]:09d}+00:00"
    return {"turn_at": timestamp, "time_source": "Gemini.turn[4]",
            "source_turn_time": value}


def parse_chatgpt(html: str, url: str) -> Conversation:
    root = None
    for raw in STREAM.findall(html):
        try:
            pool = json.loads(json.loads(raw))
        except (ValueError, TypeError):
            continue
        if not isinstance(pool, list) or not pool:
            continue
        data = _hydrate(pool, 0)
        linear = _find(data, "linear_conversation")
        if isinstance(linear, list):
            root = {"title": _find(data, "title"), "linear_conversation": linear,
                    "current_node": _find(data, "current_node")}
            break
    if not root or not root["linear_conversation"]:
        raise UnreadableShare("公开 HTML 没有可验证的 ChatGPT 对话数据")
    nodes = root["linear_conversation"]
    if root["current_node"] and root["current_node"] != nodes[-1].get("id"):
        raise UnreadableShare("ChatGPT 对话末尾缺失")
    messages = []
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise UnreadableShare("ChatGPT 对话节点损坏")
        m = node.get("message")
        if m is None and i == 0:
            continue  # graph root has no visible message
        if not isinstance(m, dict):
            raise UnreadableShare("ChatGPT 发言缺失")
        role = (m.get("author") or {}).get("role")
        meta, content = m.get("metadata") or {}, m.get("content") or {}
        if role in ("system", "developer", "tool") or meta.get("is_visually_hidden_from_conversation"):
            continue
        if content.get("content_type") in ("model_editable_context", "user_editable_context"):
            continue  # internal context, never public user/AI speech
        if role == "assistant" and (
            (isinstance(m.get("recipient"), str) and m["recipient"] != "all")
            or m.get("channel") == "analysis"
            or content.get("content_type") in ("thoughts", "reasoning_recap")
        ):
            # Shares include assistant tool calls (sometimes empty/redacted)
            # and reasoning UI nodes without the visual-hidden flag. These
            # are not user-facing replies; validate visible speech below.
            continue
        if role not in ("user", "assistant"):
            raise UnreadableShare("ChatGPT 发言角色不明")
        if meta.get("is_redacted") or meta.get("is_complete") is False or m.get("status") not in (None, "finished_successfully"):
            raise UnreadableShare("ChatGPT 发言被删减或尚未完成")
        if meta.get("attachments") or meta.get("files") or content.get("content_type") != "text":
            raise UnreadableShare("ChatGPT 会话含图片或附件，第一版只导入完整纯文字会话")
        messages.append({"role": role, "text": _text_parts(content.get("parts")), **_message_time(m)})
    return Conversation("ChatGPT", url, root["title"] or "ChatGPT 分享对话", _validate(messages), html)


def _at(value, *indices):
    for index in indices:
        if not isinstance(value, list) or index >= len(value):
            return None
        value = value[index]
    return value


def _media(value):
    if isinstance(value, (list, dict)):
        return any(_media(x) for x in (value.values() if isinstance(value, dict) else value))
    if not isinstance(value, str):
        return False
    # Observed Gemini image-generation response embeds both a placeholder and
    # actual media URLs, outside and sometimes inside the text candidate.
    return bool(re.search(r"(?:https?://[^\s<>]*googleusercontent\.com/|data:(?:image|audio|video)/|\b(?:file-service|image_generation_content)/)", value)
                or (value.startswith(("http://", "https://")) and re.search(r"\.(?:png|jpe?g|gif|webp|pdf|mp4|webm|mp3|wav)(?:[?#]|$)", value)))


def parse_gemini(raw: str, url: str) -> Conversation:
    payload = None
    for line in raw.splitlines():
        if not line.lstrip().startswith("[["):
            continue
        try:
            rows = json.loads(line)
        except ValueError:
            continue
        for row in rows:
            if isinstance(row, list) and row[:2] == ["wrb.fr", "ujx1Bf"] and len(row) > 2 and isinstance(row[2], str):
                payload = json.loads(row[2])
    root = _at(payload, 0)
    if _at(root, 3) != url.rsplit("/", 1)[-1] or not isinstance(_at(root, 1), list):
        raise UnreadableShare("Gemini 分享 ID 或对话结构不完整")
    messages = []
    for turn in root[1]:
        prompt, response = _at(turn, 2), _at(turn, 3)
        if not isinstance(prompt, list) or not isinstance(response, list):
            raise UnreadableShare("Gemini 发言结构缺失")
        if _media(turn) or any(isinstance(v, (dict, list)) and v for v in prompt[1:]):
            raise UnreadableShare("Gemini 会话含无法取得的图片、附件或未知结构，暂不完整导入")
        user = _text_parts(_at(prompt, 0))
        candidates = _at(response, 0)
        if not isinstance(candidates, list) or len(candidates) != 1:
            # Do not arbitrarily choose one answer from a branching response.
            raise UnreadableShare("Gemini 回答存在缺失或多分支，无法确定分享内容")
        answer = _text_parts(_at(candidates, 0, 1))
        timing = _turn_time(turn)
        messages.extend([{"role": "user", "text": user, **timing},
                         {"role": "assistant", "text": answer, **timing}])
    return Conversation("Gemini", url, _at(root, 2, 1) or "Gemini 分享对话", _validate(messages), raw)


def fetch(url: str) -> Conversation:
    provider, canonical = identify(url) or (None, None)
    if not provider or urlsplit(canonical).hostname == "share.gemini.google":
        canonical = resolve(url)
        provider = identify(canonical)[0]
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=25) as client:
        if provider == "ChatGPT":
            r = client.get(canonical)
        else:
            sid = canonical.rsplit("/", 1)[-1]
            rpc = json.dumps([[["ujx1Bf", json.dumps([None, sid, [4]]), None, "generic"]]])
            r = client.post("https://gemini.google.com/_/BardChatUi/data/batchexecute",
                params={"rpcids": "ujx1Bf", "source-path": "/share/" + sid, "hl": "en-US", "rt": "c"},
                data={"f.req": rpc}, headers={"X-Same-Domain": "1", "Referer": "https://gemini.google.com/"})
        r.raise_for_status()
    if len(r.content) > 8_000_000:
        raise UnreadableShare("分享响应太大，本轮不截断导入")
    return parse_chatgpt(r.text, canonical) if provider == "ChatGPT" else parse_gemini(r.text, canonical)
