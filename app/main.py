"""HTTP surface.

/telegram/webhook  save first, ack fast, hand off. Never does real work.
/tasks/process     the retryable worker, called by Cloud Tasks with an OIDC token.
/tasks/videos      one round of a durable video job, on its own queue.
"""
import asyncio
import logging
import os
import threading
import time

from fastapi import FastAPI, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import config, enrich, pipeline, store, tasks, telegram, work_budget

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("kb-bot")

app = FastAPI(title="kb-bot")

# Every client in this service (Firestore, httpx, Notion, Cloud Tasks) is
# synchronous and blocking. An `async def` route runs directly ON the event
# loop, so one slow call freezes every other in-flight request -- a 90s
# ingestion once starved incoming webhooks for 78s and Telegram gave up with
# "Read timeout expired". All blocking work therefore goes through
# run_in_threadpool; the handlers stay async only to await the request body.

# Stop retrying after this many attempts and tell the user instead of
# silently looping (plan section 25).
MAX_ATTEMPTS = 4

# An unreachable endpoint gets fewer immediate retries than a generic failure:
# the parked queue is what waits out a real outage, and every attempt before it
# re-runs the fetch, the image downloads and the (billed) OCR for nothing.
LLM_ATTEMPTS = 2


# NB: /healthz is swallowed by the Google Front End on Cloud Run and never
# reaches the container -- use a path GFE does not reserve.
@app.on_event("startup")
def _warm_clients() -> None:
    """Build the Google clients off the request path.

    Credential discovery goes through the metadata server and costs a second or
    so the first time. On a cold instance that second lands directly on the
    user's first message, so pay it in the background while the container is
    already accepting traffic.
    """
    def warm():
        for name, fn in (("firestore", store.db),
                         ("cloudtasks", tasks.client)):
            try:
                fn()
            except Exception as e:
                log.debug("warm %s skipped: %s", name, e)
    threading.Thread(target=warm, daemon=True).start()


@app.get("/_health")
def health():
    return {"ok": True}


def _allowed(msg: dict) -> bool:
    if not config.TELEGRAM_ALLOWED_USER_ID:
        return True
    return str((msg.get("from") or {}).get("id")) == config.TELEGRAM_ALLOWED_USER_ID


@app.post("/telegram/webhook")
async def webhook(request: Request,
                  x_telegram_bot_api_secret_token: str = Header(default="")):
    if config.TELEGRAM_WEBHOOK_SECRET and \
            x_telegram_bot_api_secret_token != config.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="bad secret token")

    payload = await request.json()
    update_id = payload.get("update_id")

    # Button presses. Handled inline rather than queued: a callback must be
    # answered quickly or Telegram leaves a spinner on the button, and the work
    # behind a menu is small.
    cb = payload.get("callback_query")
    if cb:
        if config.TELEGRAM_ALLOWED_USER_ID and \
                str((cb.get("from") or {}).get("id")) != config.TELEGRAM_ALLOWED_USER_ID:
            return {"ok": True, "skipped": "not allowed"}
        try:
            if (cb.get('data') or '').startswith('tg:'):
                from . import tag_ui
                # Keep both calls within the request lifetime (Cloud Run may
                # suspend CPU after responding), but never block rendering on
                # Telegram's spinner acknowledgement.
                results = await asyncio.gather(
                    run_in_threadpool(telegram.answer_callback, cb['id']),
                    run_in_threadpool(tag_ui.handle_callback, cb, acknowledge=False),
                    return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        raise result
            else:
                await run_in_threadpool(pipeline.handle_callback, cb)
        except Exception as e:
            log.exception("callback failed: %s", telegram.redact(e))
        return {"ok": True}

    msg = payload.get("message") or payload.get("edited_message")
    if update_id is None or not msg:
        return {"ok": True, "skipped": "no message"}
    if not _allowed(msg):
        log.info("ignoring update from %s", (msg.get("from") or {}).get("id"))
        return {"ok": True, "skipped": "not allowed"}

    # Commands are control, not content. Telegram sends /start by itself when a
    # chat first opens, and it must not land in the knowledge base as a thought.
    # Checked on `text` only, so a photo caption beginning with "/" still counts
    # as content. Answered here, before the ack, so there is nothing to clean up.
    cmd_text = (msg.get("text") or "").strip()
    first = cmd_text.split()[0].lower().split("@")[0] if cmd_text else ""
    # A bare /start is Telegram opening the chat, and gets the help text. A
    # /start carrying a payload came from a 「记下想法」 link on a material page,
    # and is a real action the pipeline has to see.
    handled_by_pipeline = first in ("/retry", "/llm", "/tags") or (
        first == "/cancel" and bool(msg.get("reply_to_message"))) or (
        first == "/start" and len(cmd_text.split()) > 1)
    if not payload.get("edited_message") and pipeline.is_command(cmd_text) \
            and not handled_by_pipeline:
        cmd = cmd_text.split()[0].lower().split("@")[0]
        reply = pipeline.HELP if cmd in ("/start", "/help") else \
            f"不认识的命令 {cmd}。\n\n{pipeline.HELP}"
        try:
            if cmd == '/staged':
                from . import staging_ui
                reply = await run_in_threadpool(staging_ui.report, msg['chat']['id'], cmd_text.split()[1:])
            await run_in_threadpool(
                telegram.send, msg["chat"]["id"], reply,
                reply_to=msg["message_id"])
        except Exception as e:
            log.warning("command reply failed: %s", e)
            if cmd == '/staged':
                try:
                    await run_in_threadpool(telegram.send, msg['chat']['id'],
                                            '暂存区暂时读取失败，请稍后再发 /staged。',
                                            reply_to=msg['message_id'])
                except Exception:
                    log.warning('staged read failure notification could not be sent')
        return {"ok": True, "skipped": "command"}

    # An album is N updates sharing a media_group_id, with no signal for
    # "that was the last one". The first to arrive claims the group and is the
    # only one that answers and gets queued -- claimed here, before the ack,
    # because five screenshots must not produce five 「收到」 receipts. A photo
    # that turns up long after the leader has built the page is handled on its
    # own instead of vanishing.
    # Only photo albums are collected: an album of documents would still be one
    # page each, and must not sit through the collection delay for nothing.
    group_id = msg.get("media_group_id") if (
        msg.get("photo") and not payload.get("edited_message")) else None
    role = "leader"
    if group_id:
        try:
            role = await run_in_threadpool(
                store.join_media_group, msg["chat"]["id"], group_id, update_id,
                msg["message_id"],
                max(msg["photo"], key=lambda p: p.get("file_size") or 0)["file_id"],
                (msg.get("caption") or "").strip())
        except Exception as e:
            # Grouping is an improvement on one-page-per-photo, not a
            # precondition for it. If the claim fails, fall back to the old
            # behaviour rather than losing the picture.
            log.warning("media group claim failed, handling alone: %s", e)
            role = "late"

    # 1+2. Save durably and say something, at the same time.
    #
    # These used to run in sequence, so the first visible reply waited on a
    # Firestore round trip for no reason -- the user only needs to know we have
    # the message, and the save is what makes that true. Running both together
    # costs max(save, send) instead of save + send. The wording is "收到" and
    # not "已收集" because at send time the write may still be in flight.
    async def _ack():
        if payload.get("edited_message") or role == "member":
            return None
        return await run_in_threadpool(
            telegram.send, msg["chat"]["id"], "👀 收到，正在处理…",
            reply_to=None if first == '/tags' else msg["message_id"])

    fresh, ack = await asyncio.gather(
        run_in_threadpool(store.save_update, update_id, payload),
        _ack(),
        return_exceptions=True,
    )
    if isinstance(ack, BaseException):
        log.warning("ack failed: %s", telegram.redact(ack))
        ack = None
    if isinstance(fresh, BaseException):
        raise fresh
    if not fresh:
        return {"ok": True, "skipped": "duplicate update"}
    if ack:
        # Recording the ack id is bookkeeping for the progress updates. The
        # update is already saved, so a failure here must not fail the webhook
        # and make Telegram redeliver -- worst case we lose progress edits.
        try:
            await run_in_threadpool(_record_ack, update_id, ack["message_id"])
        except Exception as e:
            log.warning("could not record ack id: %s", telegram.redact(e))

    # 3. Hand off. If the queue is unavailable, do the work inline rather than
    #    drop it -- the update is already durably stored either way.
    if role == "member":
        # Saved, recorded in the group, and that is all: the leader's task is
        # what turns the whole album into a page.
        return {"ok": True, "grouped": True}
    delay = store.MEDIA_GROUP_DELAY_SECONDS if role == "leader" and group_id else 0
    if not await run_in_threadpool(tasks.enqueue_process, update_id, 0, delay):
        log.warning("queue unavailable, processing inline")
        if group_id:
            # Inline means "right now", which would build the page before the
            # album's other photos had even arrived. The queue normally buys
            # that wait; without it, wait here. This path is already the slow
            # degraded one -- a few seconds on top of a full inline ingestion
            # is nothing next to losing half the screenshots.
            await asyncio.sleep(store.MEDIA_GROUP_DELAY_SECONDS)
        try:
            await run_in_threadpool(_process, update_id)
        except Exception as e:
            log.exception("inline processing failed: %s", e)
    return {"ok": True}


def _record_ack(update_id: int, ack_message_id: int) -> None:
    store.set_update_status(update_id, "received")
    store.db().collection("updates").document(str(update_id)).set(
        {"ack_message_id": ack_message_id}, merge=True)


def _verify_oidc(authorization: str) -> None:
    """Only Tasks/Scheduler running as our service account may call /tasks/*."""
    if not config.TASKS_SA_EMAIL:
        return
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        from google.auth.transport import requests as grequests
        from google.oauth2 import id_token
        claims = id_token.verify_oauth2_token(
            authorization.split(" ", 1)[1], grequests.Request(),
            audience=config.SERVICE_URL.rstrip("/"))
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"bad token: {e}")
    if claims.get("email") != config.TASKS_SA_EMAIL:
        raise HTTPException(status_code=403, detail="wrong service account")


@app.post("/tasks/process")
async def process_task(request: Request, authorization: str = Header(default="")):
    body = await request.json()
    await run_in_threadpool(_verify_oidc, authorization)
    update_id = body.get("update_id")
    if update_id is None:
        raise HTTPException(status_code=400, detail="no update_id")
    await run_in_threadpool(_process, update_id)
    return {"ok": True}


@app.post("/tasks/drain")
async def drain_task(request: Request, authorization: str = Header(default="")):
    await run_in_threadpool(_verify_oidc, authorization)
    return await run_in_threadpool(_drain)


@app.post("/tasks/retry")
async def retry_task(request: Request, authorization: str = Header(default="")):
    await run_in_threadpool(_verify_oidc, authorization)
    return await run_in_threadpool(_auto_retry)


def _auto_retry() -> dict:
    # Same recovery path as /retry, with no Telegram progress or daily summary.
    # /tasks/drain already handles waiting_llm every 15 minutes. Do not renew
    # parked_at here: the three-day bound must remain a real bound.
    summary, _ = pipeline._handle_retry(lambda _: None, automatic=True)
    log.info("auto-retry: %s", telegram.redact(summary))
    return {"ok": True, "summary": summary}


def _drain() -> dict:
    """Re-dispatch updates that were only ever waiting on the LLM.

    Expire first, even during an outage. Probe only before re-dispatching fresh
    entries, so an outage does not burn all their immediate retries again.
    """
    from . import image_batches, video
    try:
        image_sent = image_batches.drain()
    except Exception:
        log.exception('image continuation drain failed')
        image_sent = 0
    try:
        video_sent = video.drain()
    except Exception:
        log.exception('video continuation drain failed')
        video_sent = 0
    parked = store.waiting_for_llm()
    if not parked:
        return {"ok": True, "parked": 0, "image_sent": image_sent, "video_sent": video_sent}
    sent, expired = 0, 0
    fresh = []
    cutoff = time.time() - store.MAX_PARK_DAYS * 86400
    for row in parked:
        uid = row["update_id"]
        if (row.get("parked_at") or 0) <= cutoff:
            try:
                _expire_parked(row)
                expired += 1
            except Exception:
                # Keep waiting_llm until the durable handoff succeeds; the next
                # drain can retry it, even if the model is still unavailable.
                log.exception("drain: could not expire %s", uid)
            continue
        fresh.append(row)

    if fresh and not enrich.endpoint_alive():
        log.info("drain: endpoint still down, %s expired, %s still waiting", expired, len(fresh))
        return {"ok": True, "parked": len(parked), "sent": 0,
                "expired": expired, "reason": "endpoint down"}

    for row in fresh:
        uid = row["update_id"]
        if tasks.enqueue_process(uid, store.bump_redispatch(uid)):
            sent += 1
        else:
            log.warning("drain: could not re-enqueue %s", uid)
    log.info("drain: %s parked, %s sent, %s expired", len(parked), sent, expired)
    return {"ok": True, "parked": len(parked), "sent": sent, "expired": expired}


def _expire_parked(row: dict) -> None:
    payload = (row.get("payload") or {})
    msg = row.get("latest_message") or payload.get("message") or payload.get("edited_message") or {}
    chat = (msg.get("chat") or {}).get("id")
    uid = row["update_id"]
    url = pipeline.first_url(msg)
    text = msg.get("text") or msg.get("caption") or ""
    attachment = bool(msg.get("photo") or msg.get("document"))
    note = f"⚠️ 等待 LLM 已达 {store.MAX_PARK_DAYS} 天，已停止自动整理。"
    notion_url = ""
    if url and not attachment:
        if pipeline.chat_share.is_share(url):
            detail, notion_url = pipeline.chat_notes.stage(msg, url, text.replace(url, "", 1), "LLM 过期")
        else:
            detail, notion_url = pipeline._stage(
                msg, url, "LLM 过期", text.replace(url, "").strip(), "自动整理结果")
        note += "\n" + detail
    elif text.strip() and not pipeline.is_command(text) and not attachment:
        detail, notion_url = pipeline._handle_thought(msg, text)
        note += "\n" + detail
    else:
        note += "\n原始消息仍已保存；图片或文件请在 LLM 恢复后重新发送。"

    # Clear the lock only after the link/thought has been saved. A late Cloud
    # Tasks delivery must not resurrect this update and park it again.
    store.unpark(uid, url)
    store.set_update_status(uid, "park_expired")
    if not chat:
        return
    try:
        telegram.send(chat, note, reply_to=msg.get("message_id"),
                      notion_url=notion_url or None, parse_mode="HTML")
    except Exception:
        log.exception("could not tell the user their message stopped waiting")


@work_budget.limited(260)
def _process(update_id: int) -> None:
    doc = store.get_update(update_id)
    if not doc:
        log.warning("update %s missing", update_id)
        return
    if doc.get("status") in ("done", "park_expired"):
        return

    payload = doc["payload"]
    is_edit = "edited_message" in payload
    msg = doc.get("latest_message") or payload.get("message") or payload.get("edited_message")
    chat_id = msg["chat"]["id"]
    ack_id = doc.get("ack_message_id")
    attempts = store.bump_attempts(update_id)

    def say(note: str) -> None:
        """Keep the user posted while a slow step runs. Best effort: a failed
        status update must never fail the ingestion itself."""
        if not ack_id:
            return
        try:
            telegram.edit(chat_id, ack_id, note)
        except Exception as e:
            log.debug("progress update skipped: %s", telegram.redact(e))

    parked_url = pipeline.first_url(msg)
    try:
        store.set_update_status(update_id, "processing")
        text, url = pipeline.dispatch(msg, is_edit, say, update_id)
        store.set_update_status(update_id, "done")
        store.unpark(update_id, parked_url)
        if not text:
            # Interactive commands answer with their own message (a picker, say),
            # so the "got it" placeholder has nothing to become -- clear it
            # instead of leaving 正在处理… sitting in the chat forever.
            if ack_id:
                telegram.delete(chat_id, ack_id)
            return
        if ack_id:
            telegram.edit(chat_id, ack_id, text, notion_url=url or None,
                          parse_mode="HTML")
        else:
            telegram.send(chat_id, text, reply_to=msg["message_id"],
                          notion_url=url or None, parse_mode="HTML")
    except enrich.EndpointUnavailable as e:
        # Produce nothing rather than a page with no title, overview or tags.
        # Parked on every attempt, not only the last: when the queue is down the
        # webhook runs this inline, where attempts never reaches the threshold.
        log.warning("llm unavailable for %s (attempt %s): %s", update_id, attempts, e)
        store.park_for_llm(update_id, parked_url, error=telegram.redact(str(e))[:500])
        if attempts >= LLM_ATTEMPTS:
            try:
                note = ("⏸ 已收下，没丢。本次 LLM 请求未完成，已保留等待重试。"
                        "\n每 15 分钟自动检查；原因：" + telegram.redact(str(e))[:180])
                if ack_id:
                    telegram.edit(chat_id, ack_id, note)
                else:
                    telegram.send(chat_id, note, reply_to=msg["message_id"])
            except Exception:
                log.exception("could not tell the user their message is parked")
            return   # 200 -> Cloud Tasks stops; the drain takes it from here
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        log.exception("processing %s failed (attempt %s)", update_id, e)
        store.set_update_status(update_id, "failed", str(e))
        if attempts >= MAX_ATTEMPTS:
            # Stop the retry loop and say so, once.
            try:
                note = f"⚠️ 这条处理了 {attempts} 次都失败了，先放这儿没丢：\n{e}"
                if ack_id:
                    telegram.edit(chat_id, ack_id, note)
                else:
                    telegram.send(chat_id, note, reply_to=msg["message_id"])
            except Exception:
                log.exception("could not notify user about permanent failure")
            return   # 200 -> Cloud Tasks stops retrying
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tasks/tags")
async def tags_task(request: Request, authorization: str = Header(default="")):
    await run_in_threadpool(_verify_oidc, authorization)
    body = await request.json()
    job_id = body.get("job_id")
    if not isinstance(job_id, str) or not job_id or len(job_id) > 64:
        raise HTTPException(status_code=400, detail="bad job_id")
    from . import tag_jobs
    try:
        await run_in_threadpool(tag_jobs.process, job_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=telegram.redact(e)) from None
    return {"ok": True}


@app.post('/tasks/images')
async def images_task(request: Request, authorization: str = Header(default='')):
    await run_in_threadpool(_verify_oidc, authorization)
    body = await request.json()
    url = body.get('url')
    if not isinstance(url, str) or not url.startswith(('https://', 'http://')):
        raise HTTPException(status_code=400, detail='missing image job URL')
    from . import image_batches
    result = await run_in_threadpool(image_batches.resume, url)
    if result.get('busy'):
        raise HTTPException(status_code=503, detail='image import lease busy; retry later')
    return result


@app.post('/tasks/videos')
async def videos_task(request: Request, authorization: str = Header(default='')):
    await run_in_threadpool(_verify_oidc, authorization)
    body = await request.json()
    key = body.get('key')
    if not isinstance(key, str) or not key.startswith(('youtube:', 'bilibili:')) or len(key) > 120:
        raise HTTPException(status_code=400, detail='bad video job key')
    from . import video
    result = await run_in_threadpool(video.resume, key)
    if result.get('busy'):
        raise HTTPException(status_code=503, detail='video job lease busy; retry later')
    return result
