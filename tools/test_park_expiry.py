"""Offline acceptance tests for bounded LLM waiting and queued-link thoughts.

Exercise the real store functions against an in-memory Firestore, so URL-lock
ownership and durable staged thought IDs are checked alongside the pipeline.
"""
import copy
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import enrich, main, notion, pipeline, storage, store, tasks, telegram


class Snapshot:
    def __init__(self, key, value):
        self.id, self.value = key, copy.deepcopy(value)
        self.exists = value is not None

    def to_dict(self):
        return copy.deepcopy(self.value)


class Ref:
    def __init__(self, data, key):
        self.data, self.key = data, key

    def get(self, transaction=None):
        return Snapshot(self.key, self.data.get(self.key))

    def set(self, fields, merge=False):
        row = self.data.setdefault(self.key, {}) if merge else {}
        for name, value in fields.items():
            if isinstance(value, store.firestore.ArrayUnion):
                items = list(row.get(name, []))
                for item in value.values:
                    if item not in items:
                        items.append(copy.deepcopy(item))
                row[name] = items
            else:
                row[name] = copy.deepcopy(value)
        self.data[self.key] = row

    def delete(self):
        self.data.pop(self.key, None)


class Collection:
    def __init__(self, data, predicate=lambda row: True):
        self.data, self.predicate = data, predicate

    def document(self, key):
        return Ref(self.data, key)

    def where(self, field, op, value):
        assert op == "=="
        return Collection(self.data, lambda row: row.get(field) == value)

    def stream(self):
        return [Snapshot(key, row) for key, row in self.data.items() if self.predicate(row)]


class Transaction:
    def set(self, ref, fields, merge=False):
        ref.set(fields, merge)

    def delete(self, ref):
        ref.delete()


class Database:
    def __init__(self):
        self.data = {}

    def collection(self, name):
        return Collection(self.data.setdefault(name, {}))

    def transaction(self):
        return Transaction()


URL = "https://example.com/article"
THOUGHT = "这个结论与我的经验不同，需要再核对"


class ParkingTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.thoughts, self.materials, self.linked, self.notifications = [], [], [], []
        self.patch(store, "_db", self.db)
        # The stand-in executes transactions synchronously; production uses
        # Firestore's transaction decorator to protect the read/delete pair.
        self.patch(store.firestore, "transactional", lambda fn: fn)
        self.patch(notion, "create_thought", self.create_thought)
        self.patch(notion, "body_location", lambda page: (page["id"], ["body"]))
        self.patch(notion, "create_material", self.create_material)
        self.patch(notion, "append_blocks", lambda *a: None)
        self.patch(notion, "find_materials", lambda *a: [])
        self.patch(notion, "page_alive", lambda pid: True)
        self.patch(notion, "link_thought_to_material", lambda tid, ids: self.linked.append((tid, ids)))
        self.patch(telegram, "note_deeplink", lambda pid: "")
        self.patch(telegram, "send", lambda *a, **kw: self.notifications.append((a, kw)))
        self.patch(storage, "archive_text", lambda *a: "gs://archive/body.txt")
        self.fetch = self.patch(pipeline, "fetch_for", lambda *a: {
            "ok": True, "error": "", "title": "原始标题", "text": "完整原始正文" * 20})
        self.enrich = self.patch(enrich, "enrich", lambda *a, **kw: (
            enrich.Enrichment(title="整理标题", overview="中文概览", key_points=["原文的中文要点"]), ""))
        self.alive = self.patch(enrich, "endpoint_alive", lambda: False)
        self.enqueue = self.patch(tasks, "enqueue_process", lambda *a: True)

    def patch(self, obj, name, replacement):
        p = patch.object(obj, name, side_effect=replacement) if callable(replacement) else patch.object(obj, name, replacement)
        result = p.start()
        self.addCleanup(p.stop)
        return result

    def create_thought(self, **kw):
        self.thoughts.append(kw)
        return {"id": f"t{len(self.thoughts)}", "url": f"https://notion/t{len(self.thoughts)}"}

    def create_material(self, **kw):
        self.materials.append(kw)
        return {"id": f"m{len(self.materials)}", "url": "https://notion/material"}

    def parked(self, uid=1, text=URL, days=4, **fields):
        msg = {"chat": {"id": 7}, "message_id": uid, "text": text, **fields}
        store.save_update(uid, {"message": msg})
        store.park_for_llm(uid, pipeline.first_url(msg))
        self.db.data["updates"][str(uid)]["parked_at"] = time.time() - days * 86400
        return msg

    def test_parking_preserves_actual_request_error_and_original_wait_age(self):
        self.parked(days=1)
        original=store.get_update(1)['parked_at']
        store.park_for_llm(1,URL,error='500 request failed')
        row=store.get_update(1)
        self.assertEqual(row['error'],'500 request failed')
        self.assertEqual(row['parked_at'],original)
        self.assertEqual(row['status'],'waiting_llm')

    def test_outage_expires_then_retry_restores_full_material_and_thoughts(self):
        self.parked(text=URL + " " + THOUGHT)
        self.parked(uid=2, text="https://example.com/fresh", days=1)
        out = main._drain()
        self.assertEqual(out["expired"], 1)
        self.assertEqual(out["sent"], 0)
        self.enqueue.assert_not_called()
        self.fetch.assert_not_called()
        self.enrich.assert_not_called()
        self.assertFalse(self.materials)
        staged = store.get_staged(URL)
        self.assertEqual(staged["error"], "LLM 过期")
        self.assertFalse(staged.get("thought_ids"))
        self.assertIsNone(store.pending_url_owner(URL))
        self.assertEqual(store.get_update(1)["status"], "park_expired")
        self.assertIsNotNone(store.get_update(1)["payload"])
        self.assertEqual(store.get_update(2)["status"], "waiting_llm")
        self.assertIn("/retry", self.notifications[0][0][1])

        main._process(1)  # Late task delivery must not re-park or build a page.
        self.assertFalse(self.materials)
        result, _ = pipeline._handle_retry(lambda _: None)
        self.assertIn("1 条成功", result)
        self.assertEqual(len(self.materials), 1)
        blocks = self.materials[0]["blocks"]
        self.assertNotIn("完整原始正文", str([b for b in blocks if b["type"] != "toggle"]))
        self.assertIn("完整原始正文", str(blocks))
        self.assertIn("原文的中文要点", str(self.materials[0]["blocks"]))
        self.assertIn("gs://archive/body.txt", str(self.materials[0]["blocks"]))
        self.assertEqual(self.linked, [])
        self.assertIsNone(store.get_staged(URL))

        # /retry remaps this message to a material, but keeps the note checkpoint.
        record = store.get_record(7, 1)
        self.assertEqual(record["kind"], "material")
        self.assertNotIn("link_thought", record)

    def test_queued_duplicate_ignores_text_without_releasing_original_lock(self):
        self.parked(days=1)
        for mid in (2, 3):
            msg = {'chat': {'id': 7}, 'message_id': mid, 'text': URL + ' ' + THOUGHT}
            store.save_update(mid, {'message': msg})
            main._process(mid)
            self.assertEqual(store.get_update(mid)['payload']['message']['text'], msg['text'])
        self.assertFalse(self.thoughts)
        self.assertEqual(store.pending_url_owner(URL), 1)
        self.assertIsNone(store.get_staged(URL))
        main._process(1)
        self.assertEqual(len(self.materials), 1)
        self.assertIsNone(store.pending_url_owner(URL))

    def test_queued_share_boilerplate_and_bare_url_do_not_create_thoughts(self):
        self.parked(days=1)
        for note in ("", "- 小红书", "复制此链接打开【小红书】"):
            text, url = pipeline._handle_url({"chat": {"id": 7}, "message_id": 2}, URL, note, update_id=2)
            self.assertIn("已经在队列里", text)
            self.assertEqual(url, "")
        self.assertFalse(self.thoughts)
        self.assertIsNone(store.get_staged(URL))

    def test_expired_link_can_be_sent_again(self):
        self.parked()
        main._drain()
        pipeline._handle_url({"chat": {"id": 7}, "message_id": 3}, URL, "", update_id=3)
        self.assertEqual(len(self.materials), 1)
        self.assertIsNone(store.get_staged(URL))

    def test_recovery_expires_stale_items_before_redispatching_fresh_ones(self):
        self.parked()
        self.parked(uid=2, text="https://example.com/fresh", days=1)
        def alive():
            self.assertIsNotNone(store.get_staged(URL))
            self.assertIsNone(store.pending_url_owner(URL))
            return True
        self.alive.side_effect = alive
        out = main._drain()
        self.assertEqual((out["expired"], out["sent"]), (1, 1))
        self.enqueue.assert_called_once_with(2, 1)

    def test_failed_handoff_keeps_link_recoverable_for_next_drain(self):
        self.parked()
        with patch.object(store, "stage_url", side_effect=RuntimeError("Firestore unavailable")):
            out = main._drain()
        self.assertEqual(out["expired"], 0)
        self.assertEqual(store.get_update(1)["status"], "waiting_llm")
        self.assertEqual(store.pending_url_owner(URL), 1)
        self.assertFalse(self.notifications)
        out = main._drain()
        self.assertEqual(out["expired"], 1)
        self.assertIsNotNone(store.get_staged(URL))

    def test_failed_expiry_handoff_does_not_create_thoughts(self):
        self.parked(text=URL + ' ' + THOUGHT)
        with patch.object(store, 'stage_url', side_effect=RuntimeError('Firestore unavailable')):
            for _ in range(2):
                with self.assertLogs('kb-bot', level='ERROR'):
                    out = main._drain()
                self.assertEqual(out['expired'], 0)
                self.assertFalse(self.thoughts)
                self.assertEqual(store.pending_url_owner(URL), 1)
        self.assertEqual(main._drain()['expired'], 1)
        self.assertFalse(store.get_staged(URL).get('thought_ids'))
        result, _ = pipeline._handle_retry(lambda _: None)
        self.assertIn('1 条成功', result)
        self.assertFalse(self.thoughts)
        self.assertEqual(len(self.materials), 1)

    def test_failed_capture_staging_ignores_attached_text(self):
        msg = {'chat': {'id': 7}, 'message_id': 1}
        with patch.object(store, 'stage_url', side_effect=RuntimeError('Firestore unavailable')):
            with self.assertRaises(RuntimeError):
                pipeline._stage(msg, URL, '抓取失败', THOUGHT, '正文')
        _, note_url = pipeline._stage(msg, URL, '抓取失败', THOUGHT, '正文')
        self.assertFalse(self.thoughts)
        self.assertFalse(note_url)
        self.assertFalse(store.get_staged(URL).get('thought_ids'))

    def test_plain_thought_expiry_does_not_need_the_model(self):
        self.parked(text=THOUGHT)
        out = main._drain()
        self.assertEqual(out["expired"], 1)
        self.assertEqual(store.get_record(7, 1)["kind"], "thought")
        self.assertIn(THOUGHT, str(self.thoughts[0]["blocks"]))
        self.enrich.assert_not_called()
        self.alive.assert_not_called()

    def test_attachment_expiry_keeps_raw_message_and_does_not_stage_caption_url(self):
        self.parked(text=URL, document={"file_id": "pdf"})
        out = main._drain()
        self.assertEqual(out["expired"], 1)
        self.assertIsNone(store.get_staged(URL))
        self.assertEqual(store.get_update(1)["payload"]["message"]["document"]["file_id"], "pdf")
        self.assertIn("重新发送", self.notifications[0][0][1])
        self.assertFalse(self.materials)

    def test_staged_updates_union_thoughts_and_preserve_existing_material_id(self):
        store.stage_url(URL, 7, 1, "等待 LLM", ["t1"])
        store.keep_staged_links(URL, "material", ["t1"])
        store.stage_url(URL, 7, 2, "等待 LLM", ["t2", "t1"])
        row = store.get_staged(URL)
        self.assertEqual(row["thought_ids"], ["t1", "t2"])
        self.assertEqual(row["page_id"], "material")

    def test_daily_retry_restores_expired_link_without_duplicating_thought_or_sending(self):
        self.parked(text=URL + " " + THOUGHT)
        main._drain()
        before = len(self.notifications)
        result = main._auto_retry()
        self.assertIn("1 条成功", result["summary"])
        self.assertEqual(len(self.materials), 1)
        self.assertEqual(len(self.thoughts), 0)
        self.assertEqual(self.linked, [])
        self.assertIsNone(store.get_staged(URL))
        self.assertNotIn("link_thought", store.get_record(7, 1))
        self.assertEqual(len(self.notifications), before)
        # Re-delivery of the daily request does not create another material.
        main._auto_retry()
        self.assertEqual(len(self.materials), 1)

    def test_daily_retry_does_not_renew_parked_age_or_delete_failed_link(self):
        self.parked(days=2.99)
        original = store.get_update(1)["parked_at"]
        main._auto_retry()
        self.assertEqual(store.get_update(1)["parked_at"], original)
        self.assertEqual(store.get_update(1)["status"], "waiting_llm")
        self.assertFalse(self.materials)
        store.stage_url("https://example.com/failed", 7, 2, "抓取失败", ["existing-note"])
        self.fetch.side_effect = lambda *a, **kw: {
            "ok": False, "error": "登录提示", "title": "", "text": ""}
        result = main._auto_retry()
        self.assertIn("1 条仍未完成", result["summary"])
        self.assertEqual(store.get_staged("https://example.com/failed")["thought_ids"], ["existing-note"])
        self.assertEqual(store.get_update(1)["parked_at"], original)

    def test_daily_and_manual_retry_share_lease_and_release_on_exception(self):
        owner = store.claim_retry()
        self.assertIsNotNone(owner)
        self.assertIn("跳过", main._auto_retry()["summary"])
        self.assertIn("跳过", pipeline._handle_retry(lambda _: None)[0])
        self.fetch.assert_not_called()
        store.release_retry("different-owner")
        self.assertIsNone(store.claim_retry())
        store.release_retry(owner)
        with patch.object(pipeline, "_retry_pending", side_effect=RuntimeError("Notion down")):
            with self.assertRaises(RuntimeError):
                main._auto_retry()
        self.assertIsNotNone(store.claim_retry())

    def test_crashed_retry_lease_expires_and_old_owner_cannot_release_new_pass(self):
        old = store.claim_retry()
        self.db.data["maintenance"]["retry"]["expires_at"] = time.time() - 1
        new = store.claim_retry()
        self.assertIsNotNone(new)
        self.assertNotEqual(old, new)
        store.release_retry(old)
        self.assertIsNone(store.claim_retry())
        store.release_retry(new)

    def test_retry_route_requires_existing_oidc_identity(self):
        from fastapi.testclient import TestClient
        with patch.object(main.config, "TASKS_SA_EMAIL", "runtime@example.com"):
            with patch.object(main, "_auto_retry") as work:
                client = TestClient(main.app)
                response = client.post("/tasks/retry")
                self.assertEqual(response.status_code, 401)
                work.assert_not_called()
                with patch.object(main, "_verify_oidc", return_value=None) as auth:
                    work.return_value = {"ok": True}
                    response = client.post("/tasks/retry", headers={"Authorization": "Bearer test"})
                    self.assertEqual(response.status_code, 200)
                    auth.assert_called_once_with("Bearer test")
                    work.assert_called_once()


if __name__ == "__main__":
    unittest.main()
