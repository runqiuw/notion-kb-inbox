#!/usr/bin/env bash
# Provision + deploy kb-bot on GCP. Safe to re-run; every step is idempotent.
#
# Sizing is deliberately minimal: the service scales to zero, so an idle month
# costs nothing and a normal month stays inside the free tiers.
set -euo pipefail

: "${GCP_PROJECT:?set GCP_PROJECT}"
: "${LLM_BASE_URL:?set LLM_BASE_URL}"
: "${LLM_MODEL:?set LLM_MODEL}"
: "${NOTION_MATERIAL_DB:?set NOTION_MATERIAL_DB (see docs/setup-notion.md)}"
: "${NOTION_THOUGHT_DB:?set NOTION_THOUGHT_DB (see docs/setup-notion.md)}"
REGION="${GCP_REGION:-us-central1}"
SERVICE="${SERVICE:-kb-bot}"
QUEUE="${TASKS_QUEUE:-kb-bot-queue}"
BUCKET="${GCS_BUCKET:-${GCP_PROJECT}-kb-originals}"
SA="kb-bot-sa"
SA_EMAIL="${SA}@${GCP_PROJECT}.iam.gserviceaccount.com"

gcloud config set project "$GCP_PROJECT" >/dev/null

echo "==> enabling APIs"
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  firestore.googleapis.com cloudtasks.googleapis.com storage.googleapis.com \
  vision.googleapis.com secretmanager.googleapis.com \
  cloudscheduler.googleapis.com

echo "==> service account"
gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA" --display-name="kb-bot runtime"

for ROLE in roles/datastore.user roles/storage.objectAdmin \
            roles/cloudtasks.enqueuer roles/secretmanager.secretAccessor \
            roles/run.invoker; do
  gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
    --member="serviceAccount:${SA_EMAIL}" --role="$ROLE" \
    --condition=None >/dev/null
done

# Cloud Tasks mints an OIDC token as this same service account, which requires
# the account to be able to act as itself. Without this the enqueue 403s.
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountUser" >/dev/null

echo "==> firestore (native mode)"
gcloud firestore databases describe --database='(default)' >/dev/null 2>&1 || \
  gcloud firestore databases create --location="$REGION" --type=firestore-native

echo "==> gcs bucket"
gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1 || \
  gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" \
    --uniform-bucket-level-access

echo "==> cloud tasks queue"
gcloud tasks queues describe "$QUEUE" --location="$REGION" >/dev/null 2>&1 || \
  gcloud tasks queues create "$QUEUE" --location="$REGION"
# One worker at a time, 4 tries, then give up and let the app tell the user.
gcloud tasks queues update "$QUEUE" --location="$REGION" \
  --max-attempts=4 --max-concurrent-dispatches=1 \
  --min-backoff=10s --max-backoff=300s
# Video rounds (a 15-minute Gemini slice can take several minutes) run on their
# own queue so they never hold up new messages. The app schedules its own
# back-off; queue retries only cover a crashed or busy worker.
VIDEO_QUEUE="${VIDEO_TASKS_QUEUE:-kb-bot-video-queue}"
gcloud tasks queues describe "$VIDEO_QUEUE" --location="$REGION" >/dev/null 2>&1 || \
  gcloud tasks queues create "$VIDEO_QUEUE" --location="$REGION"
gcloud tasks queues update "$VIDEO_QUEUE" --location="$REGION" \
  --max-attempts=3 --max-concurrent-dispatches=1 \
  --min-backoff=60s --max-backoff=600s

echo "==> secrets (values are read from your shell, never written to disk)"
put_secret () {
  local NAME="$1" VALUE="$2"
  [ -z "$VALUE" ] && { echo "    skip $NAME (not set)"; return; }
  gcloud secrets describe "$NAME" >/dev/null 2>&1 || \
    gcloud secrets create "$NAME" --replication-policy=automatic
  printf '%s' "$VALUE" | gcloud secrets versions add "$NAME" --data-file=- >/dev/null
  echo "    $NAME updated"
}
put_secret kb-telegram-token   "${TELEGRAM_BOT_TOKEN:-}"
put_secret kb-telegram-secret  "${TELEGRAM_WEBHOOK_SECRET:-}"
put_secret kb-notion-token     "${NOTION_TOKEN:-}"
put_secret kb-llm-key          "${LLM_API_KEY:-}"
# Gemini free tier (YouTube transcription) + YouTube Data API: a key from a
# project WITHOUT billing, so quota runs out instead of charging.
put_secret kb-gemini-key       "${GEMINI_API_KEY:-}"

# --set-env-vars below replaces the whole set, so anything that lives only in
# the deployed service is wiped by a re-run of this script that did not happen
# to export it. The Zhihu device cookie is exactly that: it is copied out of a
# browser, not stored in any file here, and losing it is invisible -- 知乎 links
# just start falling into 「等待采集」 again. Carry forward whatever is there.
keep_env() {
  local out
  out="$(gcloud run services describe "$SERVICE" --region "$REGION" --format=json 2>/dev/null |
    python3 -c 'import json, sys
env = []
try:
    env = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env", [])
except Exception:
    pass
print(next((e.get("value", "") for e in env if e.get("name") == sys.argv[1]), ""))' "$1" 2>/dev/null || true)"
  printf '%s' "$out"
}
ZHIHU_DC0="${ZHIHU_DC0:-$(keep_env ZHIHU_DC0)}"
S2_API_KEY="${S2_API_KEY:-$(keep_env S2_API_KEY)}"
CROSSREF_MAILTO="${CROSSREF_MAILTO:-$(keep_env CROSSREF_MAILTO)}"
DYNAMIC_WEB_ENABLED="${DYNAMIC_WEB_ENABLED:-$(keep_env DYNAMIC_WEB_ENABLED)}"
DYNAMIC_WEB_TIMEOUT="${DYNAMIC_WEB_TIMEOUT:-$(keep_env DYNAMIC_WEB_TIMEOUT)}"
USER_TIMEZONE="${USER_TIMEZONE:-$(keep_env USER_TIMEZONE)}"
LLM_VLLM_HOSTS="${LLM_VLLM_HOSTS:-$(keep_env LLM_VLLM_HOSTS)}"

# Optional secrets (e.g. the Gemini key for video) are bound only once they exist,
# otherwise Cloud Run refuses the whole revision.
SECRETS="TELEGRAM_BOT_TOKEN=kb-telegram-token:latest,TELEGRAM_WEBHOOK_SECRET=kb-telegram-secret:latest,NOTION_TOKEN=kb-notion-token:latest,LLM_API_KEY=kb-llm-key:latest"
for PAIR in GEMINI_API_KEY=kb-gemini-key; do
  gcloud secrets describe "${PAIR#*=}" >/dev/null 2>&1 && SECRETS="${SECRETS},${PAIR}:latest"
done

echo "==> deploy"
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --service-account "$SA_EMAIL" \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=3 \
  --memory=1Gi \
  --cpu=1 \
  --concurrency=10 \
  --timeout=900 \
  --set-env-vars="GCP_PROJECT=${GCP_PROJECT},GCP_REGION=${REGION},GCS_BUCKET=${BUCKET},TASKS_QUEUE=${QUEUE},TASKS_SA_EMAIL=${SA_EMAIL},NOTION_MATERIAL_DB=${NOTION_MATERIAL_DB},NOTION_THOUGHT_DB=${NOTION_THOUGHT_DB},TELEGRAM_ALLOWED_USER_ID=${TELEGRAM_ALLOWED_USER_ID:-},LLM_BASE_URL=${LLM_BASE_URL},LLM_MODEL=${LLM_MODEL},LLM_USE_SCHEMA=${LLM_USE_SCHEMA:-1},ZHIHU_DC0=${ZHIHU_DC0:-},CROSSREF_MAILTO=${CROSSREF_MAILTO:-},S2_API_KEY=${S2_API_KEY:-},DYNAMIC_WEB_ENABLED=${DYNAMIC_WEB_ENABLED:-1},DYNAMIC_WEB_TIMEOUT=${DYNAMIC_WEB_TIMEOUT:-35},USER_TIMEZONE=${USER_TIMEZONE:-Asia/Shanghai},LLM_VLLM_HOSTS=${LLM_VLLM_HOSTS:-}" \
  --set-secrets="$SECRETS"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo "==> service url: $URL"

# SERVICE_URL is needed by the app itself (Cloud Tasks target + OIDC audience),
# so set it now that we know it.
gcloud run services update "$SERVICE" --region "$REGION" \
  --update-env-vars="SERVICE_URL=${URL}" >/dev/null

# Keep one instance warm. A request every 10 minutes stops Cloud Run from
# reclaiming the instance, so the first message of the day does not pay a 5-6s
# cold start. Billed only for the ~0.1s each ping is handled (~350 vCPU-seconds
# a month against a 180,000 free allowance), where --min-instances=1 would bill
# an idle instance around the clock for $6-10.
echo "==> keep-alive heartbeat"
if ! gcloud scheduler jobs describe kb-bot-keepalive --location="$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs create http kb-bot-keepalive \
    --location="$REGION" --schedule="*/10 * * * *" \
    --uri="${URL}/_health" --http-method=GET \
    --attempt-deadline=30s --max-retry-attempts=1 \
    --description="Keeps one Cloud Run instance warm."
else
  gcloud scheduler jobs update http kb-bot-keepalive \
    --location="$REGION" --schedule="*/10 * * * *" --uri="${URL}/_health" >/dev/null
fi

# Messages that arrived while the LLM endpoint was down are parked rather than
# filed as broken pages. This is what wakes them up again. It probes the
# endpoint first, so running against a still-dead endpoint costs one tiny call.
echo "==> parked-message drain"
if ! gcloud scheduler jobs describe kb-bot-drain --location="$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs create http kb-bot-drain \
    --location="$REGION" --schedule="*/15 * * * *" \
    --uri="${URL}/tasks/drain" --http-method=POST \
    --oidc-service-account-email="$SA_EMAIL" --oidc-token-audience="$URL" \
    --attempt-deadline=120s --max-retry-attempts=1 \
    --description="Re-dispatches updates parked waiting for the LLM."
else
  gcloud scheduler jobs update http kb-bot-drain \
    --location="$REGION" --schedule="*/15 * * * *" \
    --uri="${URL}/tasks/drain" \
    --oidc-service-account-email="$SA_EMAIL" --oidc-token-audience="$URL" >/dev/null
fi

echo "==> daily staged-material retry"
RETRY_VERB=create
if gcloud scheduler jobs describe kb-bot-retry-daily --location="$REGION" >/dev/null 2>&1; then
  RETRY_VERB=update
fi
gcloud scheduler jobs "$RETRY_VERB" http kb-bot-retry-daily \
  --location="$REGION" --schedule="10 3 * * *" --time-zone="${USER_TIMEZONE:-Asia/Shanghai}" \
  --uri="${URL}/tasks/retry" --http-method=POST \
  --oidc-service-account-email="$SA_EMAIL" --oidc-token-audience="$URL" \
  --attempt-deadline=300s --max-retry-attempts=1 \
  --description="Retries staged links and unfinished materials daily, independently of backup."

echo "==> pointing Telegram at the service"
SERVICE_URL="$URL" python3 tools/set_webhook.py

echo
echo "done. url: $URL"
