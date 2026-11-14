"""In-memory stand-in for the Firestore staging area, shared by the tests."""


def install(store) -> dict:
    staged: dict[str, dict] = {}
    records: dict[str, dict] = {}

    def stage_url(url, chat_id, message_id, error, thought_ids=None):
        row = staged.setdefault(store.normalize_url(url), {
            "url": url, "chat_id": chat_id, "message_id": message_id,
            "attempts": 0, "thought_ids": []})
        row["error"] = error
        row["attempts"] += 1
        row["thought_ids"] += thought_ids or []
        return row["attempts"]

    store.stage_url = stage_url
    store.get_record = lambda chat, message: records.get(store.record_id(chat, message))
    store.put_record = lambda chat, message, **fields: records.setdefault(
        store.record_id(chat, message), {}).update(fields) or store.record_id(chat, message)
    store.keep_staged_links = lambda url, page_id, ids: staged[store.normalize_url(url)].update(
        {"page_id": page_id, "thought_ids": list(ids), "error": "关联或显示未完成"})
    store.get_staged = lambda url: staged.get(store.normalize_url(url))
    store.update_chat_stage = lambda url, **fields: staged.setdefault(store.normalize_url(url), {}).update(fields)
    store.staged_urls = lambda: list(staged.values())
    store.unstage = lambda url: staged.pop(store.normalize_url(url), None)
    store.claim_retry = lambda: "test-retry-owner"
    store.release_retry = lambda owner: None
    return staged
