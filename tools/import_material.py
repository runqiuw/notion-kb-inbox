"""Run the existing ingestion path for an administrative verification sample.

No Telegram update is sent or forged. A local PDF uses the same already-received
bytes adapter as Telegram's downloader. Real extraction and LLM calls are kept.
"""

import os
import argparse
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from import_chat import configure_local
from app import notion, pipeline, store


def main():
    p = argparse.ArgumentParser(description=__doc__)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url")
    src.add_argument("--pdf-path")
    p.add_argument("--project", default=os.environ.get("GCP_PROJECT"), required=not os.environ.get("GCP_PROJECT"))
    p.add_argument("--local-gcloud", action="store_true")
    args = p.parse_args()
    if args.local_gcloud:
        configure_local(args.project)
    source = args.url or str(Path(args.pdf_path).resolve())
    mid = "cover-import-" + store.url_hash(source)
    msg = {"chat": {"id": 0}, "message_id": mid}
    if args.url:
        msg["text"] = args.url
        result, url = pipeline.dispatch(
            msg, False, lambda text: print(text, flush=True)
        )
    else:
        path = Path(args.pdf_path)
        data = path.read_bytes()
        if not data.startswith(b"%PDF") or len(data) > 20 * 1024 * 1024:
            raise ValueError("not a bounded PDF")
        msg["document"] = {
            "file_id": mid,
            "file_name": path.name,
            "mime_type": "application/pdf",
        }
        old = store.get_record(0, mid)
        if (
            old
            and old.get("notion_page_id")
            and notion.page_alive(old["notion_page_id"])
        ):
            result, url = "已有验收样本", old["notion_url"]
        else:
            with patch("app.telegram.download", return_value=(data, path.name)):
                result, url = pipeline.dispatch(
                    msg, False, lambda text: print(text, flush=True)
                )
    row = store.get_record(0, mid)
    if row and row.get("notion_page_id"):
        print(
            json.dumps(
                {
                    "result": result,
                    "notion_url": url,
                    "page_id": row["notion_page_id"],
                    "sample": "封面与 icon 验收；可与其他测试内容一起清理",
                },
                ensure_ascii=False,
            )
        )
    else:
        print(json.dumps({"result": result, "notion_url": url}, ensure_ascii=False))
    return 0 if url else 1


if __name__ == "__main__":
    raise SystemExit(main())
