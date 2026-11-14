"""Conservative caption-anchored PDF display crop, isolated from the bot.

Render the whole figure region, including vectors and labels; never extract the
first embedded raster object. Ambiguous layouts fall back to the first page.
"""

from contextlib import closing
import io
import re
import subprocess
import sys

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 12


def _lines(tp):
    text = tp.get_text_range()
    out = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped:
            start = offset + len(line) - len(line.lstrip())
            try:
                boxes = [
                    tp.get_charbox(i)
                    for i in range(
                        start, min(offset + len(line.rstrip()), tp.count_chars())
                    )
                ]
                boxes = [b for b in boxes if b[2] > b[0] and b[3] > b[1]]
                if boxes:
                    out.append(
                        (
                            stripped,
                            (
                                min(b[0] for b in boxes),
                                min(b[1] for b in boxes),
                                max(b[2] for b in boxes),
                                max(b[3] for b in boxes),
                            ),
                        )
                    )
            except (ValueError, IndexError):
                pass
        offset += len(line)
    return out


def figure_box(page):
    w, h = page.get_size()
    with closing(page.get_textpage()) as tp:
        lines = _lines(tp)
    captions = [
        (s, b)
        for s, b in lines
        if re.match(r"^(?:Figure|Fig\.?|图)\s*\d+\s*[:.：]", s, re.I)
    ]
    if not captions:
        return None, False
    _, cap = sorted(captions, key=lambda x: (-x[1][3], x[1][0]))[0]
    # Explicit caption column. A centered caption with full-width body stays
    # full-width; no attempt to split the actual contents of a figure.
    right_lines = sum(1 for s, b in lines if len(s) > 55 and b[0] > w * 0.48)
    if cap[0] > w * 0.48:
        left, right = w * 0.49, w * 0.95
    elif cap[0] < w * 0.2 and cap[2] < w * 0.54 and right_lines > 4:
        left, right = w * 0.05, w * 0.51
    else:
        left, right = w * 0.05, w * 0.95
    bottom = cap[3] + 4
    # A full prose line above the caption is a hard boundary. Figure labels
    # don't establish a boundary; all panel/axis text is rendered together.
    prose = [
        b[1]
        for s, b in lines
        if len(s) > 65 and b[0] >= left - 4 and b[2] <= right + 4 and b[1] > bottom + 12
    ]
    top = min(prose) - 5 if prose else h * 0.94
    objects = []
    # Only page-level objects: form XObjects have transformed overall bounds;
    # child bounds can be in local coordinates and must not be mixed in.
    for obj in page.get_objects(max_depth=1):
        b = obj.get_bounds()
        if (
            obj.type == 1
            or b[1] < bottom
            or b[3] > top
            or b[0] < left - 8
            or b[2] > right + 8
        ):
            continue
        if b[2] - b[0] < 2 or b[3] - b[1] < 2:
            continue
        objects.append(b)
    if not objects:
        return None, True
    box = (
        min(b[0] for b in objects),
        min(b[1] for b in objects),
        max(b[2] for b in objects),
        max(b[3] for b in objects),
    )
    if box[1] - bottom > 45 or box[2] - box[0] < 100 or box[3] - box[1] < 70:
        return None, True
    # Any intersecting object across the boundary makes a crop uncertain.
    for obj in page.get_objects(max_depth=1):
        b = obj.get_bounds()
        if (
            obj.type != 1
            and b[3] > bottom
            and b[1] < top
            and b[2] > box[0]
            and b[0] < box[2]
            and (b[1] < bottom or b[3] > top)
            and b[3] - b[1] > 5
        ):
            return None, True
    labels = [
        b
        for s, b in lines
        if b[1] >= box[1] - 12
        and b[3] <= box[3] + 12
        and b[0] >= left
        and b[2] <= right
    ]
    return (
        max(left, min([box[0], *[b[0] for b in labels]]) - 8),
        max(bottom, box[1] - 8),
        min(right, max([box[2], *[b[2] for b in labels]]) + 8),
        min(top, box[3] + 8),
    ), True


def render(data):
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(data) as doc:
        if not len(doc):
            return None, {"mode": "failed"}
        chosen = None
        for i in range(min(len(doc), MAX_PAGES)):
            with closing(doc[i]) as page:
                if max(page.get_size()) > 2400:
                    break
                box, found = figure_box(page)
                if found:
                    if box:
                        w, h = page.get_size()
                        # PDF coordinates originate at bottom left. PDFium
                        # crop is (left,bottom,right,top), in PDF page units.
                        chosen = (
                            page.render(
                                scale=2, crop=(box[0], box[1], w - box[2], h - box[3])
                            )
                            .to_pil()
                            .copy()
                        )
                        mode = {"mode": "figure", "page": i + 1, "box": list(box)}
                    break  # ambiguous first figure -> first-page fallback
        if chosen is None:
            with closing(doc[0]) as page:
                if max(page.get_size()) > 2400:
                    return None, {"mode": "failed"}
                chosen = (
                    page.render(scale=min(2, 1400 / max(page.get_size())))
                    .to_pil()
                    .copy()
                )
                mode = {"mode": "first_page", "page": 1}
        buf = io.BytesIO()
        chosen.convert("RGB").save(buf, "JPEG", quality=88)
        return buf.getvalue(), mode


def pdf_thumbnail(data):
    if not data or len(data) > MAX_PDF_BYTES:
        return None
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "app.pdf_art"],
            input=data,
            capture_output=True,
            timeout=16,
        )
        return (
            proc.stdout
            if proc.returncode == 0 and proc.stdout.startswith(b"\xff\xd8")
            else None
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


if __name__ == "__main__":
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
        # On Linux PDFium/Pillow are isolated and have a hard allocation cap.
        if sys.platform == "linux":
            resource.setrlimit(resource.RLIMIT_AS, (320 * 1024**2, 320 * 1024**2))
        blob = sys.stdin.buffer.read(MAX_PDF_BYTES + 1)
        if len(blob) > MAX_PDF_BYTES:
            raise ValueError("oversized PDF")
        image, meta = render(blob)
        if not image:
            raise ValueError("no image")
        sys.stdout.buffer.write(image)
    except Exception:
        sys.exit(1)
