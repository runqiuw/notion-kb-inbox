"""Deterministic card previews; never invent media duration or full-text access."""
import re


def card_metadata(blocks: list[dict], material_type: str) -> dict[str, str]:
    overview: list[str] = []
    section = ""

    def walk(items):
        nonlocal section
        for block in items:
            kind = block.get("type", "")
            body = block.get(kind, {})
            text = "".join(t.get("text", {}).get("content", t.get("plain_text", ""))
                           for t in body.get("rich_text", []))
            if kind.startswith("heading"):
                if text.startswith("中文概览"):
                    section = "overview"
                else:
                    section = ""
            elif kind in {"paragraph", "code"}:
                if section == "overview":
                    overview.append(text)
            # Ignore decoration/error details; include folded original text.
            if body.get("children") and (kind != "toggle" or text.startswith("展开全文")):
                walk(body["children"])

    walk(blocks)
    summary = re.sub(r"\s+", " ", " ".join(overview)).strip()
    preview = re.split(r"(?<=[。！？])", summary, maxsplit=1)[0]
    preview = preview[:89] + "…" if len(preview) > 90 else preview
    return {"卡片摘要": preview}
