"""Install a daily Cloud Run backup job AFTER a verified live restore drill.

Usage: .venv/bin/python tools/schedule_backup.py --report /path/to/drill.json
No changes to the Telegram service, production databases, or original bucket.
"""
import os
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import backup, config  # noqa: E402


def gc(*args, capture=False):
    for attempt in range(5):
        binding = "add-iam-policy-binding" in args
        p = subprocess.run(["gcloud", *args], text=True,
                           stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.PIPE if binding else None)
        # A newly created service account may not yet be visible to other IAM
        # APIs. Repeating this binding is idempotent; job executions are not.
        if binding and p.returncode and "does not exist" in (p.stderr or "") and attempt < 4:
            time.sleep(2 ** (attempt + 1))
            continue
        if p.returncode:
            if p.stderr:
                print(p.stderr, file=sys.stderr)
            raise subprocess.CalledProcessError(p.returncode, p.args)
        return p.stdout


def validated_report(path):
    r = json.loads(Path(path).read_text())
    c = r.get("checks", {})
    # Older reports lack the mirror count: keep demanding the shared-edit drill.
    # Newer ones demand it only when the restored group has mirrors to edit.
    mirrors = c.get("synced_mirrors_in_group")
    shared_ok = (c.get("shared_note_references", 0) > 0 and c.get("shared_edit_verified") is True) \
        if mirrors is None or mirrors > 0 else True
    if not (r.get("status") == "verified" and c.get("pages", 0) >= 2
            and c.get("firestore_mappings", 0) > 0 and c.get("gcs_objects", 0) > 0 and shared_ok
            and c.get("notion_attachment_blocks", 0) + c.get("gcs_attachments_visible_in_notion", 0) > 0):
        raise ValueError("A successful live restore, attachment and shared-edit drill is required before scheduling")
    roles = {kind for kind, _ in backup.databases()}
    if "pages_by_role" in c and not roles <= set(c["pages_by_role"]):
        raise ValueError("The restore drill must include a page from every backed-up database: "
                         + ", ".join(sorted(roles - set(c["pages_by_role"]))))
    return r


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report", required=True)
    p.add_argument("--project", default=os.environ.get("GCP_PROJECT"), required=not os.environ.get("GCP_PROJECT"))
    p.add_argument("--region", default="us-central1")
    p.add_argument("--time-zone", default=os.environ.get("USER_TIMEZONE", "Asia/Shanghai"))
    args = p.parse_args()
    validated_report(args.report)
    project, region = args.project, args.region
    common = ["--project=" + project, "--quiet"]
    service = json.loads(gc("run", "services", "describe", "kb-bot", "--region=" + region,
                            "--format=json", *common, capture=True))
    env = {e["name"]: e["value"] for e in service["spec"]["template"]["spec"]["containers"][0]["env"] if "value" in e}
    bucket, sa = project + "-kb-backups", "kb-bot-backup-sa@" + project + ".iam.gserviceaccount.com"
    exists = subprocess.run(["gcloud", "iam", "service-accounts", "describe", sa, *common],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if exists.returncode:
        gc("iam", "service-accounts", "create", "kb-bot-backup-sa", "--display-name=KB backup reader", *common)
    gc("projects", "add-iam-policy-binding", project, "--member=serviceAccount:" + sa,
       "--role=roles/datastore.viewer", "--condition=None", *common, capture=True)
    gc("storage", "buckets", "add-iam-policy-binding", "gs://" + env["GCS_BUCKET"],
       "--member=serviceAccount:" + sa, "--role=roles/storage.objectViewer", *common, capture=True)
    gc("storage", "buckets", "add-iam-policy-binding", "gs://" + bucket,
       "--member=serviceAccount:" + sa, "--role=roles/storage.objectCreator", *common, capture=True)
    gc("secrets", "add-iam-policy-binding", "kb-notion-token", "--member=serviceAccount:" + sa,
       "--role=roles/secretmanager.secretAccessor", *common, capture=True)
    tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    image = f"{region}-docker.pkg.dev/{project}/cloud-run-source-deploy/kb-bot-backup:{tag}"
    gc("builds", "submit", ".", "--tag=" + image, "--region=" + region, *common)
    job_env = {name: env[name] for name in ("GCS_BUCKET", "NOTION_MATERIAL_DB", "NOTION_THOUGHT_DB")}
    job_env.update({name: getattr(config, name) for name in (
        "NOTION_LIFE_MATERIAL_DB", "NOTION_LIFE_NOTE_DB", "NOTION_WIKI_DB", "NOTION_WIKI_LOG_DB")})
    job_env.update(GCP_PROJECT=project, BACKUP_BUCKET=bucket, NOTION_VERSION=env.get("NOTION_VERSION", "2022-06-28"))
    gc("run", "jobs", "deploy", "kb-bot-backup", "--image=" + image, "--region=" + region,
       "--service-account=" + sa, "--command=python", "--args=-m,app.backup,create",
       "--set-env-vars=" + ",".join(f"{k}={v}" for k, v in job_env.items()),
       "--set-secrets=NOTION_TOKEN=kb-notion-token:latest", "--tasks=1", "--parallelism=1",
       "--max-retries=1", "--task-timeout=1800s", "--memory=512Mi", "--cpu=1", *common)
    # First prove the deployed job's identity can read all three stores and
    # publish an archive. Only then install/enable its recurring trigger.
    gc("run", "jobs", "execute", "kb-bot-backup", "--region=" + region, "--wait", *common)
    gc("run", "jobs", "add-iam-policy-binding", "kb-bot-backup", "--region=" + region,
       "--member=serviceAccount:" + sa, "--role=roles/run.invoker", *common, capture=True)
    exists = subprocess.run(["gcloud", "scheduler", "jobs", "describe", "kb-bot-backup-daily",
                             "--location=" + region, *common], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    verb = "update" if exists.returncode == 0 else "create"
    gc("scheduler", "jobs", verb, "http", "kb-bot-backup-daily", "--location=" + region,
       "--schedule=0 3 * * *", "--time-zone=" + args.time_zone, "--http-method=POST",
       f"--uri=https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/kb-bot-backup:run",
       ("--update-headers=" if verb == "update" else "--headers=") + "Content-Type=application/json", "--message-body={}",
       "--oauth-service-account-email=" + sa, "--oauth-token-scope=https://www.googleapis.com/auth/cloud-platform",
       "--attempt-deadline=180s", "--max-retry-attempts=1", *common)
    print("Daily backup installed: 03:00 " + args.time_zone + "; gs://" + bucket + "/snapshots/")


if __name__ == "__main__":
    main()
