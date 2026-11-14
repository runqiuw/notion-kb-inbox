"""Runtime-switchable LLM provider, stored in Firestore.

Cloud Run is stateless and scales to zero, so "which provider is active" cannot
live in process memory or an env var the user would have to redeploy to change.
It lives in one Firestore document that the bot can rewrite from Telegram --
the point being that when the shared cluster is down, switching to a paid API
is a chat message, not a deploy.
"""
import logging
import time
from typing import Any

from . import config, store

log = logging.getLogger(__name__)

_DOC = ("settings", "llm")
_CACHE: dict[str, Any] = {"at": 0.0, "value": None}
_CACHE_TTL = 20.0   # seconds; long enough to spare Firestore, short enough that
                    # a switch takes effect on the next message


def _ref():
    return store.db().collection(_DOC[0]).document(_DOC[1])


def _env_provider() -> dict:
    """What deploy.sh configured. Always available as a fallback so the bot
    still works before anything has been registered from Telegram."""
    return {"name": "default", "base_url": config.LLM_BASE_URL,
            "model": config.LLM_MODEL, "api_key": config.LLM_API_KEY,
            "from_env": True}


def _load() -> dict:
    now = time.monotonic()
    if _CACHE["value"] is not None and now - _CACHE["at"] < _CACHE_TTL:
        return _CACHE["value"]
    try:
        snap = _ref().get()
        data = snap.to_dict() if snap.exists else {}
    except Exception as e:
        log.warning("settings read failed, using env: %s", e)
        data = {}
    _CACHE.update(at=now, value=data)
    return data


def _invalidate() -> None:
    _CACHE.update(at=0.0, value=None)


def active() -> dict:
    """The provider to use right now."""
    data = _load()
    name = data.get("active")
    providers = data.get("providers") or {}
    if name and name in providers:
        p = dict(providers[name])
        p["name"] = name
        p.setdefault("api_key", "")
        p["from_env"] = False
        return p
    return _env_provider()


def providers() -> dict:
    return dict(_load().get("providers") or {})


def add(name: str, base_url: str, model: str, api_key: str) -> None:
    _ref().set({"providers": {name: {"base_url": base_url.rstrip("/"),
                                     "model": model, "api_key": api_key,
                                     "added_at": time.time()}}}, merge=True)
    _invalidate()


def use(name: str) -> bool:
    if name == "default":
        _ref().set({"active": None}, merge=True)
        _invalidate()
        return True
    if name not in providers():
        return False
    _ref().set({"active": name}, merge=True)
    _invalidate()
    return True


def remove(name: str) -> bool:
    from google.cloud import firestore

    if name not in providers():
        return False
    _ref().update({f"providers.{name}": firestore.DELETE_FIELD})
    if _load().get("active") == name:
        _ref().set({"active": None}, merge=True)
    _invalidate()
    return True


def set_model(model: str) -> str:
    """Change the model on the active provider without re-entering its key."""
    data = _load()
    name = data.get("active")
    if not name or name not in (data.get("providers") or {}):
        return ""   # the env provider's model is fixed at deploy time
    _ref().set({"providers": {name: {"model": model}}}, merge=True)
    _invalidate()
    return name


def save_picks(key: str, items: list[str], trigger: int = 0) -> None:
    """Remember what a picker's buttons stand for.

    Telegram allows 64 bytes of callback_data, far too little for an arbitrary
    model id, so buttons carry an index and the list is looked up here.
    """
    store.db().collection("ui").document(key).set(
        {"items": items, "trigger": trigger, "at": time.time()})


def load_picks(key: str) -> tuple[list[str], int]:
    """Returns (items, trigger message id)."""
    try:
        snap = store.db().collection("ui").document(key).get()
        if not snap.exists:
            return [], 0
        d = snap.to_dict() or {}
        return list(d.get("items") or []), int(d.get("trigger") or 0)
    except Exception as e:
        log.warning("pick list read failed: %s", e)
        return [], 0


def list_models(provider: dict | None = None) -> tuple[list[str], str]:
    """Ask the provider what it serves. Returns (models, error)."""
    import httpx

    p = provider or active()
    if not p.get("base_url"):
        return [], "当前没有配置端点"
    try:
        r = httpx.get(p["base_url"].rstrip("/") + "/models",
                      headers={"Authorization": f"Bearer {p.get('api_key', '')}"},
                      timeout=30.0)
        if r.status_code >= 400:
            return [], f"HTTP {r.status_code}"
        data = r.json().get("data") or []
        ids = sorted({m.get("id", "") for m in data if m.get("id")})
        return ids, "" if ids else "端点没有返回任何模型"
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def redact_key(key: str) -> str:
    if not key:
        return "（未设置）"
    return f"{key[:4]}…{key[-4:]}" if len(key) > 12 else "已设置"
