"""Firestore state. This is the system of record for processing, not Notion.

Collections
  updates/{update_id}   raw Telegram update, written before any processing;
                        latest_message preserves edits while waiting for LLM
  records/{record_id}   message -> Notion page, keyed by chat:message;
                        link_thought checkpoints an attached note separately,
                        so material mapping does not erase its saved page ID
  urls/{url_hash}       dedupe index -> record_id
  pending/{chat_id}     material a "记下想法" deep link armed, until the next message
  pending_urls/{hash}   URL parked waiting for the LLM -> update_id, so the same
                        link sent twice does not queue twice
  media_groups/{id}     one Telegram album -> its member photos, so N updates
                        become one page
  maintenance/retry    expiring lease shared by manual and daily recovery
  video_jobs/{hash}    one video entity: stage, checkpoints, retry schedule
"""
import hashlib
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from google.cloud import firestore

_db: Optional[firestore.Client] = None


def db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


# Tracking params that must not make two identical links look different.
_JUNK_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "spm", "share_token", "share_source", "from_source",
    "ref", "refer", "share_from", "vd_source", "_r", "si",
}


def normalize_url(url: str) -> str:
    """Canonical form used for dedupe. Conservative: never drops path or fragment
    meaning, only strips tracking noise and a trailing slash."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if k.lower() not in _JUNK_PARAMS])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()[:32]


def record_id(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


# ---------- updates ----------

def save_update(update_id: int, payload: dict) -> bool:
    """Write the raw update. Returns False if we have seen this update_id before
    (Telegram retries webhooks, and Cloud Tasks can double-deliver)."""
    ref = db().collection("updates").document(str(update_id))
    snap = ref.get()
    if snap.exists:
        return False
    ref.set({"payload": payload, "received_at": time.time(), "status": "received"})
    return True


def get_update(update_id: int) -> Optional[dict]:
    snap = db().collection("updates").document(str(update_id)).get()
    return snap.to_dict() if snap.exists else None


def set_update_status(update_id: int, status: str, error: str = "") -> None:
    db().collection("updates").document(str(update_id)).set(
        {"status": status, "error": error, "updated_at": time.time()}, merge=True
    )


def set_latest_message(update_id: int, message: dict) -> None:
    """Use an edited pending message on retry without erasing the raw payload."""
    db().collection("updates").document(str(update_id)).set(
        {"latest_message": message, "updated_at": time.time()}, merge=True)


# After this long, links move to staging for /retry instead of waiting forever.
MAX_PARK_DAYS = 3


def park_for_llm(update_id: int, url: str = "", error: str = "") -> None:
    """Remember that this update is only waiting on the LLM.

    Written on every failed attempt, not just the last one -- when the queue is
    unavailable the webhook processes inline, where attempts never climbs to the
    give-up threshold, and an update parked only at that threshold would never
    be parked at all.
    """
    now = time.time()
    ref = db().collection("updates").document(str(update_id))
    snap = ref.get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    first = data.get("parked_at")
    ref.set({"status": "waiting_llm", "parked_at": first or now,
             "updated_at": now, "error": error}, merge=True)
    if url:
        db().collection("pending_urls").document(url_hash(url)).set(
            {"update_id": update_id, "ts": now})
        payload = data.get("payload") or {}
        msg = payload.get("message") or payload.get("edited_message") or {}
        if msg.get("chat") and msg.get("message_id") and not (msg.get("photo") or msg.get("document")):
            put_record(msg["chat"]["id"], msg["message_id"], waiting_update_id=update_id)


def unpark(update_id: int, url: str = "") -> None:
    client = db()
    update = client.collection("updates").document(str(update_id))
    if not url:
        update.set({"parked_at": None}, merge=True)
        return

    @firestore.transactional
    def release(transaction):
        ref = client.collection("pending_urls").document(url_hash(url))
        snap = ref.get(transaction=transaction)
        # A duplicate link can finish saving its thought while the original
        # is still waiting. It must not release the original's URL lock.
        if snap.exists and (snap.to_dict() or {}).get("update_id") == update_id:
            transaction.delete(ref)
        transaction.set(update, {"parked_at": None}, merge=True)

    release(client.transaction())


def pending_url_owner(url: str) -> Optional[int]:
    """The update already queued for this URL, or None."""
    snap = db().collection("pending_urls").document(url_hash(url)).get()
    return (snap.to_dict() or {}).get("update_id") if snap.exists else None


def waiting_for_llm() -> list[dict]:
    """Parked updates, oldest first.

    Filtered on one field and sorted in memory on purpose: Firestore would want
    a composite index for filter-plus-order, and this queue is never long
    enough to need one.
    """
    docs = db().collection("updates").where("status", "==", "waiting_llm").stream()
    rows = []
    for d in docs:
        row = d.to_dict() or {}
        row["update_id"] = int(d.id)
        rows.append(row)
    rows.sort(key=lambda r: r.get("parked_at") or 0)
    return rows


def failed_updates() -> list[dict]:
    """Unresolved processing failures; successful retries change the status."""
    return [s.to_dict() or {} for s in
            db().collection('updates').where('status', '==', 'failed').stream()]


def bump_redispatch(update_id: int) -> int:
    """Cloud Tasks keeps a finished task's name for about an hour and refuses a
    duplicate, so every re-dispatch needs a fresh name. This is that counter."""
    ref = db().collection("updates").document(str(update_id))
    snap = ref.get()
    n = (snap.to_dict() or {}).get("redispatch", 0) + 1 if snap.exists else 1
    ref.set({"redispatch": n, "attempts": 0}, merge=True)
    return n


def bump_attempts(update_id: int) -> int:
    ref = db().collection("updates").document(str(update_id))
    snap = ref.get()
    n = (snap.to_dict() or {}).get("attempts", 0) + 1 if snap.exists else 1
    ref.set({"attempts": n}, merge=True)
    return n


# ---------- records ----------

def put_record(chat_id: int, message_id: int, **fields: Any) -> str:
    rid = record_id(chat_id, message_id)
    fields.setdefault("created_at", time.time())
    fields["chat_id"] = chat_id
    fields["message_id"] = message_id
    db().collection("records").document(rid).set(fields, merge=True)
    return rid


def get_record(chat_id: int, message_id: int) -> Optional[dict]:
    snap = db().collection("records").document(record_id(chat_id, message_id)).get()
    return snap.to_dict() if snap.exists else None


# ---------- url dedupe ----------

# A tap the user never followed up on must not quietly attach next week's
# unrelated thought to that material.
PENDING_NOTE_TTL_SECONDS = 3600


def set_pending_note(chat_id: int, page_id: str) -> str:
    token = uuid.uuid4().hex
    db().collection("pending").document(str(chat_id)).set(
        {"notion_page_id": page_id, "ts": time.time(), "token": token})
    return token


def cancel_pending_note(chat_id: int, token: str) -> bool:
    """Cancel only this prompt; an older button cannot cancel a newer entry."""
    client = db()
    ref = client.collection("pending").document(str(chat_id))

    @firestore.transactional
    def cancel(transaction):
        snap = ref.get(transaction=transaction)
        data = (snap.to_dict() or {}) if snap.exists else {}
        if not token or data.get("token") != token:
            return False
        transaction.delete(ref)
        return time.time() - (data.get("ts") or 0) <= PENDING_NOTE_TTL_SECONDS

    return cancel(client.transaction())


def take_pending_note(chat_id: int) -> Optional[str]:
    """Pop the material a deep link armed, or None. Always consumes, so a stale
    arming cannot survive the next message either way."""
    client = db()
    ref = client.collection("pending").document(str(chat_id))

    @firestore.transactional
    def take(transaction):
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        transaction.delete(ref)
        if time.time() - (data.get("ts") or 0) > PENDING_NOTE_TTL_SECONDS:
            return None
        return data.get("notion_page_id") or None

    return take(client.transaction())


def find_by_url(url: str) -> Optional[dict]:
    snap = db().collection("urls").document(url_hash(url)).get()
    if not snap.exists:
        return None
    rid = (snap.to_dict() or {}).get("record_id")
    if not rid:
        return None
    rsnap = db().collection("records").document(rid).get()
    return rsnap.to_dict() if rsnap.exists else None


def forget_url(url: str) -> None:
    """Drop a dedupe entry so the link can be saved again."""
    db().collection("urls").document(url_hash(url)).delete()


def index_url(url: str, rid: str) -> None:
    db().collection("urls").document(url_hash(url)).set(
        {"record_id": rid, "url": normalize_url(url), "created_at": time.time()}
    )


# ---------- staging ----------

def claim_retry() -> str | None:
    """Serialize manual and scheduled retry passes across Cloud Run instances."""
    client = db()
    ref = client.collection("maintenance").document("retry")
    owner = uuid.uuid4().hex

    @firestore.transactional
    def claim(transaction):
        snap = ref.get(transaction=transaction)
        if snap.exists and (snap.to_dict() or {}).get("expires_at", 0) > time.time():
            return None
        # Longer than the request ceiling; a crashed worker cannot lock future
        # daily runs forever. Acquiring this never changes an item's parked_at.
        transaction.set(ref, {"owner": owner, "expires_at": time.time() + 600})
        return owner

    return claim(client.transaction())


def release_retry(owner: str) -> None:
    client = db()
    ref = client.collection("maintenance").document("retry")

    @firestore.transactional
    def release(transaction):
        snap = ref.get(transaction=transaction)
        if snap.exists and (snap.to_dict() or {}).get("owner") == owner:
            transaction.delete(ref)

    release(client.transaction())


# A link whose body could not be fetched stays here instead of in Notion, so
# every page in the knowledge base can be assumed to carry its content. The
# entry is durable and says why it failed; /retry drains it.

def stage_url(url: str, chat_id: int, message_id: int, error: str,
              thought_ids: list[str] | None = None) -> int:
    ref = db().collection("staged").document(url_hash(url))
    snap = ref.get()
    old = (snap.to_dict() or {}) if snap.exists else {}
    now = time.time()
    attempts = old.get("attempts", 0) + 1
    fields = {
        "url": url, "chat_id": old.get("chat_id", chat_id),
        "message_id": old.get("message_id", message_id),
        "error": error, "attempts": attempts,
        "first_at": old.get("first_at", now), "last_at": now,
    }
    if thought_ids:
        # Concurrent messages about the same queued URL must keep every thought.
        # An empty ArrayUnion is invalid; omit the field to preserve existing IDs.
        fields["thought_ids"] = firestore.ArrayUnion(thought_ids)
    ref.set(fields, merge=True)
    return attempts


def keep_staged_links(url: str, page_id: str, thought_ids: list[str]) -> None:
    """Some thoughts still need their relation or shared body on this material."""
    db().collection("staged").document(url_hash(url)).set(
        {"page_id": page_id, "thought_ids": thought_ids,
         "error": "资料已入库，想法关联或显示未完成", "last_at": time.time()}, merge=True)


def get_staged(url: str) -> Optional[dict]:
    snap = db().collection("staged").document(url_hash(url)).get()
    return snap.to_dict() if snap.exists else None


def update_chat_stage(url: str, **fields) -> None:
    db().collection("staged").document(url_hash(url)).set(fields, merge=True)


def claim_chat_import(url: str) -> str | None:
    client = db()
    ref = client.collection("staged").document(url_hash(url))
    owner = uuid.uuid4().hex

    @firestore.transactional
    def claim(transaction):
        snap = ref.get(transaction=transaction)
        row = snap.to_dict() or {} if snap.exists else {}
        if row.get("chat_expires_at", 0) > time.time():
            return None
        transaction.set(ref, {"chat_owner": owner, "chat_expires_at": time.time() + 600}, merge=True)
        return owner

    return claim(client.transaction())


def release_chat_import(url: str, owner: str) -> None:
    client = db()
    ref = client.collection("staged").document(url_hash(url))

    @firestore.transactional
    def release(transaction):
        snap = ref.get(transaction=transaction)
        if snap.exists and (snap.to_dict() or {}).get("chat_owner") == owner:
            transaction.set(ref, {"chat_owner": None, "chat_expires_at": 0}, merge=True)

    release(client.transaction())


def staged_urls() -> list[dict]:
    rows = [s.to_dict() or {} for s in db().collection("staged").stream()]
    return sorted(rows, key=lambda r: r.get("first_at", 0))


def unstage(url: str) -> None:
    db().collection("staged").document(url_hash(url)).delete()


# ---------- media groups ----------

# A Telegram album arrives as N separate updates that share a media_group_id,
# and the API never says which one is last. The first to arrive claims the
# group; it is the only one that answers and the only one that gets queued, and
# it waits out this window so the others can record themselves before it builds
# a single page from all of them.
MEDIA_GROUP_DELAY_SECONDS = 6
# Past this the leader has almost certainly already built the page. A straggler
# is then handled on its own rather than dropped: one extra page is exactly
# what happened before grouping existed and you can see it, whereas a silently
# lost screenshot is the failure this project does not accept.
MEDIA_GROUP_LATE_SECONDS = MEDIA_GROUP_DELAY_SECONDS + 15


def media_group_key(chat_id: int, group_id: str) -> str:
    return f"{chat_id}:{group_id}"


def join_media_group(chat_id: int, group_id: str, update_id: int,
                     message_id: int, file_id: str, caption: str) -> str:
    """Record this photo in its album and say what this update has to do.

    Returns "leader" (answer the user, queue the whole group), "member" (stay
    quiet, the leader will collect this photo) or "late" (handle alone).

    The claim is a document create, not a read-then-write: album members arrive
    within milliseconds of each other and a check-then-set would let two of
    them both believe they were first, which is two pages -- the exact bug this
    is here to remove.
    """
    from google.api_core import exceptions as gexc

    ref = db().collection("media_groups").document(media_group_key(chat_id, group_id))
    member = {"update_id": update_id, "message_id": message_id,
              "file_id": file_id, "caption": caption}
    try:
        ref.create({"chat_id": chat_id, "group_id": str(group_id),
                    "leader_update_id": update_id, "members": [member],
                    "created_at": time.time()})
        return "leader"
    except gexc.AlreadyExists:
        pass
    snap = ref.get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    if time.time() - (data.get("created_at") or 0) > MEDIA_GROUP_LATE_SECONDS:
        return "late"
    ref.update({"members": firestore.ArrayUnion([member])})
    return "member"


def media_group_members(chat_id: int, group_id: str) -> list[dict]:
    """Everyone in the album, in the order the user picked them.

    Telegram message ids are monotonic within a chat, so sorting by them undoes
    the arrival order -- the stored array is in whichever order the concurrent
    webhooks happened to win.
    """
    snap = db().collection("media_groups").document(
        media_group_key(chat_id, group_id)).get()
    members = ((snap.to_dict() or {}).get("members") or []) if snap.exists else []
    return sorted(members, key=lambda m: m.get("message_id") or 0)


# Resumable image imports; heavyweight OCR/results live in the originals bucket.
def get_image_job(url: str) -> Optional[dict]:
    snap = db().collection('image_jobs').document(url_hash(url)).get()
    return snap.to_dict() if snap.exists else None


def update_image_job(job_url: str, **fields) -> None:
    db().collection('image_jobs').document(url_hash(job_url)).set(
        {**fields, 'updated_at': time.time()}, merge=True)


def claim_image_job(url: str) -> str | None:
    client = db()
    ref = client.collection('image_jobs').document(url_hash(url))
    owner = uuid.uuid4().hex
    @firestore.transactional
    def claim(tx):
        snap = ref.get(transaction=tx)
        row = snap.to_dict() or {} if snap.exists else {}
        if row.get('lease_expires', 0) > time.time():
            return None
        tx.set(ref, {'lease_owner': owner, 'lease_expires': time.time() + 360}, merge=True)
        return owner
    return claim(client.transaction())


def release_image_job(url: str, owner: str) -> None:
    client = db()
    ref = client.collection('image_jobs').document(url_hash(url))
    @firestore.transactional
    def release(tx):
        snap = ref.get(transaction=tx)
        if snap.exists and (snap.to_dict() or {}).get('lease_owner') == owner:
            tx.set(ref, {'lease_owner': None, 'lease_expires': 0}, merge=True)
    release(client.transaction())


def pending_image_jobs() -> list[dict]:
    return [s.to_dict() for s in db().collection('image_jobs').stream()
            if (s.to_dict() or {}).get('status') != 'done']


# Resumable video imports, keyed by the video entity (youtube:ID or
# bilibili:BV:cid) rather than whichever link variant was shared.
def get_video_job(key: str) -> Optional[dict]:
    snap = db().collection('video_jobs').document(url_hash(key)).get()
    return snap.to_dict() if snap.exists else None


def update_video_job(job_key: str, **fields) -> None:
    db().collection('video_jobs').document(url_hash(job_key)).set(
        {**fields, 'key': job_key, 'updated_at': time.time()}, merge=True)


def claim_video_job(key: str) -> str | None:
    client = db()
    ref = client.collection('video_jobs').document(url_hash(key))
    owner = uuid.uuid4().hex
    @firestore.transactional
    def claim(tx):
        snap = ref.get(transaction=tx)
        row = snap.to_dict() or {} if snap.exists else {}
        if row.get('lease_expires', 0) > time.time():
            return None
        # Longer than a video round (840 s budget, 900 s request ceiling).
        tx.set(ref, {'key': key, 'lease_owner': owner, 'lease_expires': time.time() + 960}, merge=True)
        return owner
    return claim(client.transaction())


def release_video_job(key: str, owner: str) -> None:
    client = db()
    ref = client.collection('video_jobs').document(url_hash(key))
    @firestore.transactional
    def release(tx):
        snap = ref.get(transaction=tx)
        if snap.exists and (snap.to_dict() or {}).get('lease_owner') == owner:
            tx.set(ref, {'lease_owner': None, 'lease_expires': 0}, merge=True)
    release(client.transaction())


# Gemini free-tier requests used per model and quota day. Every request is
# reserved here before it is sent, whatever its answer turns out to be.
def take_gemini_request(model: str, day: str, limit: int) -> int | None:
    """Reserve one request; the new count, or None when the day is spent."""
    client = db()
    ref = client.collection('gemini_quota').document(f'{model}:{day}')
    @firestore.transactional
    def take(tx):
        snap = ref.get(transaction=tx)
        count = (snap.to_dict() or {}).get('count', 0) if snap.exists else 0
        if count >= limit:
            return None
        tx.set(ref, {'model': model, 'day': day, 'count': count + 1, 'updated_at': time.time()}, merge=True)
        return count + 1
    return take(client.transaction())


def spend_gemini_day(model: str, day: str, limit: int) -> None:
    """Google says the day is spent (e.g. another client used it): stop asking."""
    ref = db().collection('gemini_quota').document(f'{model}:{day}')
    snap = ref.get()
    count = (snap.to_dict() or {}).get('count', 0) if snap.exists else 0
    ref.set({'model': model, 'day': day, 'count': max(count, limit), 'exhausted_at': time.time(),
             'updated_at': time.time()}, merge=True)


def pending_video_jobs() -> list[dict]:
    return [s.to_dict() for s in db().collection('video_jobs').stream()
            if (s.to_dict() or {}).get('status') not in ('done', 'staged')]
