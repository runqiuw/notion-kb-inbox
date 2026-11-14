"""Environment configuration. Everything is read once at import."""
import os


def _req(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise RuntimeError(f"missing required env var: {name}")
    return v


# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
# Single-user bot: ignore everyone else. Empty means "allow all" (dev only).
TELEGRAM_ALLOWED_USER_ID = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "")
# Only needed to build t.me deep links. Left empty, the bot asks getMe once.
TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "")

# Notion
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_MATERIAL_DB = os.environ.get("NOTION_MATERIAL_DB", "")
NOTION_THOUGHT_DB = os.environ.get("NOTION_THOUGHT_DB", "")
NOTION_VERSION = os.environ.get("NOTION_VERSION", "2022-06-28")
# Optional extra databases the bot never writes but the backup should cover
# (e.g. a second library or a personal wiki). Empty skips one.
NOTION_LIFE_MATERIAL_DB = os.environ.get("NOTION_LIFE_MATERIAL_DB", "")
NOTION_LIFE_NOTE_DB = os.environ.get("NOTION_LIFE_NOTE_DB", "")
NOTION_WIKI_DB = os.environ.get("NOTION_WIKI_DB", "")
NOTION_WIKI_LOG_DB = os.environ.get("NOTION_WIKI_LOG_DB", "")

# LLM: an OpenAI-format endpoint the user supplies. Only /v1/chat/completions is
# used -- this endpoint has no /v1/responses.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")   # e.g. https://host/v1
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
# Try response_format=json_schema first. Set to 0 if the gateway rejects it;
# the code falls back to prompt-only JSON automatically either way.
LLM_USE_SCHEMA = os.environ.get("LLM_USE_SCHEMA", "1") == "1"
# One slow call is bad; two is unforgivable. Cloud Run cuts the request at 300s.
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))
# Hostnames of vLLM-style gateways. DeepSeek thinking is switched off there via
# chat_template_kwargs; the official API's `thinking` field is ignored by vLLM.
LLM_VLLM_HOSTS = set(os.environ.get("LLM_VLLM_HOSTS", "").replace(",", " ").split())

# GCP
GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
TASKS_QUEUE = os.environ.get("TASKS_QUEUE", "kb-bot-queue")
SERVICE_URL = os.environ.get("SERVICE_URL", "")
TASKS_SA_EMAIL = os.environ.get("TASKS_SA_EMAIL", "")

# Crossref asks callers to identify themselves; doing so gets the faster pool.
CROSSREF_MAILTO = os.environ.get("CROSSREF_MAILTO", "kb-bot@example.com")
# Optional, free from semanticscholar.org/product/api. Without it the anonymous
# pool 429s almost immediately, so venue and citation count are usually blank.
S2_API_KEY = os.environ.get("S2_API_KEY", "")

# Zhihu device cookie. Not a login credential -- see extract.parse_cookie_for.
ZHIHU_DC0 = os.environ.get("ZHIHU_DC0", "")
# Optional dedicated XHS read session. Bind through Secret Manager; never archive it.
XHS_COMMENT_COOKIE = os.environ.get('XHS_COMMENT_COOKIE', '')

# Behaviour
MAX_TG_FILE_BYTES = 20 * 1024 * 1024  # Telegram Bot API hard limit for getFile
FETCH_TIMEOUT = 25.0
DYNAMIC_WEB_ENABLED = os.environ.get("DYNAMIC_WEB_ENABLED", "1") == "1"
DYNAMIC_WEB_TIMEOUT = min(35.0, float(os.environ.get("DYNAMIC_WEB_TIMEOUT", "35")))
# Anonymous only; enabling this never enables API keys or paid fallbacks.
JINA_READER_ENABLED = os.environ.get('JINA_READER_ENABLED', '1') == '1'
JINA_READER_TIMEOUT = min(60.0, float(os.environ.get('JINA_READER_TIMEOUT', '60')))

# Each image call is independently bounded and checkpointed.
IMAGE_LLM_TIMEOUT = min(150.0, float(os.environ.get('IMAGE_LLM_TIMEOUT', '120')))

# Video. Gemini free tier only: the key belongs to a project without billing, and
# quota or overload means waiting or staging, never a paid fallback. The same
# key reads YouTube Data API metadata (title, channel, duration).
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.8-flash')
# Free-tier requests per model per day. Failed (503/429) requests count too, and
# the day resets at Pacific midnight (both measured 10-16).
GEMINI_DAILY_REQUESTS = int(os.environ.get('GEMINI_DAILY_REQUESTS', '20'))
GEMINI_QUOTA_TZ = 'America/Los_Angeles'
# Times in replies are given in the user's local time.
USER_TIMEZONE = os.environ.get('USER_TIMEZONE', 'Asia/Shanghai')
# Long video rounds get their own queue so they never hold up new messages.
VIDEO_TASKS_QUEUE = os.environ.get('VIDEO_TASKS_QUEUE', 'kb-bot-video-queue')
