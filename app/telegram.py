"""Thin Telegram Bot API client.

The bot token sits in the URL path, so any log line carrying a Telegram URL
leaks full control of the bot. httpx logs every request URL at INFO, which put
the token straight into Cloud Logging -- hence the silencing below and the
redaction on every error path.
"""
import html
import logging
import re
import time

import httpx

from . import config

API = "https://api.telegram.org/bot{token}/{method}"

# httpx logs "HTTP Request: POST https://api.telegram.org/bot<TOKEN>/..." at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def redact(text: str) -> str:
    """Strip any bot token out of a string before it reaches a log or a user."""
    return _TOKEN_RE.sub("bot<REDACTED>", str(text))


def _call(method: str, *, request_timeout: float | None = None, **params):
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    started = time.monotonic()
    try:
        r = httpx.post(url, json=params, timeout=config.FETCH_TIMEOUT if request_timeout is None else request_timeout)
        r.raise_for_status()
    except httpx.HTTPError as e:
        # The exception message embeds the URL, and therefore the token.
        raise RuntimeError(f"telegram {method} failed: {redact(e)}") from None
    finally:
        elapsed = time.monotonic() - started
        if elapsed >= 1:
            log.warning('telegram API slow method=%s elapsed=%.2fs', method, elapsed)
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"telegram {method} failed: {redact(body)}")
    return body["result"]


def _open_button(url: str, label: str = "打开 Notion"):
    return {"inline_keyboard": [[{"text": label, "url": url}]]}


def esc(text) -> str:
    """Escape dynamic content for parse_mode="HTML". Every piece of text that
    did not come from one of our own literal templates (a title, a URL, an
    error message, anything from Notion or the user) must go through this
    before it lands inside an HTML-formatted message -- otherwise a stray
    '<', '>' or '&' either breaks Telegram's parser (400) or is read as
    formatting nobody intended."""
    return html.escape(str(text), quote=True)


def bold(text) -> str:
    return f"<b>{esc(text)}</b>"


def mono(text) -> str:
    return f"<code>{esc(text)}</code>"


def link(text, url: str) -> str:
    return f'<a href="{esc(url)}">{esc(text)}</a>'


# Telegram caps callback_data at 64 bytes, which is why every picker sends an
# index and looks the real value up rather than carrying it inline.
CALLBACK_DATA_LIMIT = 64


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """rows of (label, callback_data)."""
    return {"inline_keyboard": [
        [{"text": label, "callback_data": data} for label, data in row]
        for row in rows]}


def send(chat_id: int, text: str, reply_to: int | None = None,
         notion_url: str | None = None, label: str = "打开 Notion",
         markup: dict | None = None, parse_mode: str | None = None) -> dict:
    params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_to:
        params["reply_to_message_id"] = reply_to
        params["allow_sending_without_reply"] = True
    if markup is not None:
        params["reply_markup"] = markup
    elif notion_url:
        params["reply_markup"] = _open_button(notion_url, label)
    if parse_mode:
        params["parse_mode"] = parse_mode
    return _call("sendMessage", **params)


def edit(chat_id: int, message_id: int, text: str,
         notion_url: str | None = None, label: str = "打开 Notion",
         markup: dict | None = None, parse_mode: str | None = None) -> dict:
    params = {"chat_id": chat_id, "message_id": message_id, "text": text,
              "disable_web_page_preview": True}
    if markup is not None:
        params["reply_markup"] = markup
    elif notion_url:
        params["reply_markup"] = _open_button(notion_url, label)
    if parse_mode:
        params["parse_mode"] = parse_mode
    return _call("editMessageText", **params)


def delete(chat_id: int, message_id: int) -> None:
    """Best effort: a message may already be gone, or too old to delete."""
    try:
        _call("deleteMessage", chat_id=chat_id, message_id=message_id)
    except Exception as e:
        log.debug("deleteMessage skipped: %s", redact(e))


def answer_callback(callback_query_id: str, text: str = "") -> None:
    """Always call this, or the button keeps showing a spinner."""
    try:
        _call("answerCallbackQuery", callback_query_id=callback_query_id,
              text=text[:200], request_timeout=2.0)
    except Exception as e:
        log.debug("answerCallbackQuery skipped: %s", redact(e))


# Deep-link payloads are capped at 64 bytes and may only hold [A-Za-z0-9_-],
# which a dashless Notion page id (32 hex characters) fits with room to spare.
NOTE_START_PREFIX = "n"

_me_username: str | None = None


def me_username() -> str:
    """The bot's @name, needed to build t.me links. Cached because it cannot
    change within a process and getMe is a network round trip."""
    global _me_username
    if _me_username is not None:
        return _me_username
    name = config.TELEGRAM_BOT_USERNAME
    if not name and config.TELEGRAM_BOT_TOKEN:
        try:
            name = _call("getMe").get("username", "") or ""
        except Exception as e:
            log.warning("getMe failed, deep links disabled: %s", redact(e))
            name = ""
    _me_username = name
    return name


def note_deeplink(page_id: str) -> str:
    """Opens this chat primed to write a note about that material.

    Returns "" when the bot name is unknown -- a missing entry is better than a
    link that lands the user in an empty chat with no idea what went wrong.
    """
    name = me_username()
    if not name:
        return ""
    return f"https://t.me/{name}?start={NOTE_START_PREFIX}{page_id.replace('-', '')}"


def set_my_commands(commands: list[tuple[str, str]]) -> dict:
    """Register the slash-command menu so typing "/" offers a list."""
    return _call("setMyCommands", commands=[
        {"command": c.lstrip("/"), "description": d} for c, d in commands])


def download(file_id: str) -> tuple[bytes, str]:
    """Returns (content, remote_path). Raises if the file exceeds the Bot API's
    20 MB getFile ceiling -- callers must surface that to the user, not swallow it."""
    info = _call("getFile", file_id=file_id)
    size = info.get("file_size") or 0
    if size > config.MAX_TG_FILE_BYTES:
        raise ValueError(
            f"文件 {size / 1048576:.1f}MB 超过 Telegram Bot API 的 20MB 下载上限"
        )
    path = info["file_path"]
    url = f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{path}"
    try:
        r = httpx.get(url, timeout=60.0)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise RuntimeError(f"下载文件失败: {redact(e)}") from None
    return r.content, path


def set_webhook(url: str, secret: str) -> dict:
    return _call(
        "setWebhook",
        url=url,
        secret_token=secret,
        allowed_updates=["message", "edited_message", "callback_query"],
        drop_pending_updates=False,
    )
