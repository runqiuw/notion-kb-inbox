"""Reject loading placeholders and headings without any body content."""
import re

_PENDING = r"(?:loading|please wait|加载中|正在加载)[.!。…\s]*"


def content_pending(text, headings=()):
    # DOM textContent may join a heading and spinner without a separator.
    # Match complete heading/placeholder tokens, never arbitrary substrings or
    # a minimum article length (legitimate short articles remain admissible).
    text = re.sub(r"\s+", " ", text).strip()
    headings = sorted({re.sub(r"\s+", " ", h).strip() for h in headings if h.strip()}, key=len, reverse=True)
    while text:
        if re.fullmatch(rf"(?:{_PENDING})+", text, re.I):
            return True
        for heading in headings:
            if text.startswith(heading):
                text = text[len(heading):].strip()
                break
            if text.endswith(heading):
                text = text[:-len(heading)].strip()
                break
        else:
            return False
    return True
