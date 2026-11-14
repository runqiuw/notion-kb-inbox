"""Display categories only; these never change admission or database types."""

ART_CATEGORIES = (
    "paper",
    "article",
    "tutorial",
    "document",
    "post",
    "image",
    "video",
    "audio",
    "other",
)


def fallback_category(material_type, suggested="", *, platform="", source_kind=""):
    # Received photos and the dedicated bibliographic path are reliable hints.
    if source_kind == "paper":
        return "paper"
    if source_kind == "image" or material_type == "图片":
        return "image"
    if material_type in {"视频", "播客"}:
        return {"视频": "video", "播客": "audio"}[material_type]
    if suggested in ART_CATEGORIES and suggested != "other":
        return suggested
    # A PDF is not automatically an academic paper. Use the model's existing
    # type when it identified one; unclassified received documents stay generic.
    if material_type == "论文":
        return "paper"
    if source_kind == "document":
        return "document"
    if suggested == "other":
        return "other"
    if material_type == "帖子" or platform in {"小红书", "知乎"}:
        return "post"
    if material_type == "网页" or platform == "微信":
        return "article"
    return "other"
