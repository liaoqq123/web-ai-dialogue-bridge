import concurrent.futures
import tempfile
from pathlib import Path
import unittest

from dialogue_bridge.errors import BridgeError
from dialogue_bridge.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, key="task-key-0001"):
        return self.store.create_task("Example", {"amount": 2}, [], key)

    def started(self, key="task-key-0001"):
        task = self.task(key)
        event, claimed = self.store.claim_event()
        self.assertEqual(claimed["id"], task["id"])
        self.store.finish_event(event["id"], {"thread_id": "thread-" + task["id"], "adapter": "demo"})
        return self.store.get_task(task["id"])

    def asked(self):
        task = self.started()
        return self.store.publish_questions(task["id"], [{"id": "quantity", "title": "How many?", "options": ["1", "2"]}], "questions-0001")

    def reply(self, task, key="answer-key-0001", text="3"):
        return self.store.reply(task["id"], task["question_group"]["revision"],
                                [{"question_id": "quantity", "text": text}], key)

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(BridgeError) as ctx:
            function(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)

    def test_create_is_durable_and_idempotent(self):
        task = self.task()
        again = self.task()
        self.assertEqual(task["id"], again["id"])
        reloaded = Store(Path(self.tmp.name)).get_task(task["id"])
        self.assertEqual(reloaded, again)
        self.assertEqual(len(reloaded["events"]), 1)
        self.assert_error("idempotency_conflict", self.store.create_task, "Other", {}, [], "task-key-0001")

    def test_create_identity_uses_file_content_not_internal_storage_name(self):
        metadata = {"name": "example.txt", "storage_name": "blob-one", "sha256": "0" * 64, "size": 1}
        first = self.store.create_task("Files", {}, [metadata], "file-key-0001")
        second = self.store.create_task("Files", {}, [{**metadata, "storage_name": "blob-two"}], "file-key-0001")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["files"][0]["storage_name"], "blob-one")

    def test_reply_and_callback_commit_together_and_hide_question(self):
        asked = self.asked()
        answered = self.reply(asked)
        self.assertEqual(answered["question_group"]["status"], "answered")
        self.assertEqual(answered["status"], "running")
        event = answered["events"][-1]
        self.assertEqual(event["type"], "dialogue.reply")
        self.assertEqual(event["answers"], answered["question_group"]["answers"])
        self.assertEqual(event["id"], answered["question_group"]["event_id"])
        again = self.reply(asked)
        self.assertEqual(len(again["events"]), 2)
        self.assert_error("idempotency_conflict", self.reply, asked, text="4")

    def test_question_replacement_fences_old_browser(self):
        asked = self.asked()
        same = self.store.publish_questions(asked["id"], asked["question_group"]["questions"], "questions-0002")
        self.assertEqual(same["revision"], asked["revision"])
        changed = self.store.publish_questions(asked["id"], [{"id": "quantity", "title": "How many units?"}], "questions-0003")
        self.assertEqual(changed["revision"], asked["revision"] + 1)
        self.assertEqual(changed["history"][0]["status"], "superseded")
        self.assert_error("stale_questions", self.reply, asked)

    def test_parallel_double_submit_creates_one_event(self):
        task = self.asked()
        def attempt(key):
            try:
                return self.reply(task, key=key)
            except BridgeError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ["parallel-key-0001", "parallel-key-0002"]))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn("stale_questions", results)
        self.assertEqual(len(self.store.get_task(task["id"])["events"]), 2)

    def test_answers_must_exactly_match(self):
        asked = self.asked()
        for answers in ([], [{"question_id": "other", "text": "yes"}],
                        [{"question_id": "quantity", "text": " "}],
                        [{"question_id": "quantity", "text": "yes"}] * 2):
            self.assert_error("invalid_input", self.store.reply, asked["id"], asked["revision"], answers, "invalid-key-0001")

    def test_next_question_waits_until_reply_delivered(self):
        asked = self.asked()
        answered = self.reply(asked)
        self.assert_error("reply_not_delivered", self.store.publish_questions,
                          asked["id"], [{"id": "next", "title": "Next?"}], "next-question-01")
        event, _ = self.store.claim_event()
        self.store.finish_event(event["id"], answered["thread"])
        next_task = self.store.publish_questions(asked["id"], [{"id": "next", "title": "Next?"}], "next-question-01")
        self.assertEqual(next_task["revision"], 2)
        self.assertEqual(next_task["history"][-1]["answers"][0]["text"], "3")

    def test_reply_cannot_target_another_conversation(self):
        asked = self.asked()
        self.reply(asked)
        event, _ = self.store.claim_event()
        self.assert_error("thread_mismatch", self.store.finish_event, event["id"],
                          {"thread_id": "different-thread", "adapter": "demo"})
        self.assertEqual(self.store.get_task(asked["id"])["events"][-1]["status"], "sending")
        self.store.fail_event(event["id"], "Mismatched result", uncertain=True)
        self.assert_error("event_state", self.store.retry_event, asked["id"], event["id"])

    def test_restart_does_not_replay_inflight_start(self):
        task = self.task()
        event, _ = self.store.claim_event()
        other = Store(Path(self.tmp.name))
        other.recover_inflight()
        self.assertIsNone(other.claim_event())
        self.assertEqual(other.get_task(task["id"])["events"][0]["status"], "unknown")
        self.assert_error("event_state", other.retry_event, task["id"], event["id"])
        reconciled = other.resolve_event(task["id"], event["id"],
                                         {"thread_id": "verified-existing-thread", "adapter": "demo"})
        self.assertEqual(reconciled["thread"]["thread_id"], "verified-existing-thread")
        self.assertEqual(reconciled["events"][0]["status"], "delivered")
        self.assertIn("resolved_manually_at", reconciled["events"][0])

    def test_confirmed_failure_retry_keeps_event_identity(self):
        task = self.task()
        first, _ = self.store.claim_event()
        self.store.fail_event(first["id"], "Callback service unavailable")
        self.store.retry_event(task["id"], first["id"])
        second, _ = self.store.claim_event()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["attempts"], 2)

    def test_events_are_task_scoped(self):
        first = self.task("task-key-0001")
        second = self.task("task-key-0002")
        event, _ = self.store.claim_event()
        self.store.fail_event(event["id"], "Unavailable")
        self.assert_error("not_found", self.store.retry_event, second["id"], event["id"])
        self.assertEqual(self.store.get_task(first["id"])["events"][0]["status"], "failed")

    def test_only_one_event_per_task_can_be_sending(self):
        task = self.task()
        event, _ = self.store.claim_event()
        asked = self.store.publish_questions(task["id"], [{"id": "quantity", "title": "How many?"}], "questions-0001")
        self.reply(asked)
        self.assertIsNone(self.store.claim_event())
        self.store.finish_event(event["id"], {"thread_id": "original-thread", "adapter": "demo"})
        reply_event, resumed = self.store.claim_event()
        self.assertEqual(reply_event["type"], "dialogue.reply")
        self.assertEqual(resumed["thread"]["thread_id"], "original-thread")

    def test_complete_preserves_result_and_blocks_new_work(self):
        task = self.asked()
        complete = self.store.complete(task["id"], {"summary": "Finished"}, "complete-key-0001")
        self.assertEqual(complete["status"], "completed")
        self.assertIsNone(complete["question_group"])
        self.assertEqual(complete["result"]["summary"], "Finished")
        self.assertEqual(complete, self.store.complete(task["id"], {"summary": "Finished"}, "complete-key-0001"))
        self.assert_error("task_completed", self.store.publish_questions, task["id"], [{"id": "late", "title": "Late?"}], "late-question-01")
        self.assert_error("task_completed", self.reply, task)

    def test_completion_cancels_pending_callback_and_late_receipt_cannot_reopen(self):
        pending = self.task()
        complete = self.store.complete(pending["id"], {}, "complete-key-0001")
        self.assertEqual(complete["events"][0]["status"], "cancelled")
        self.assertIsNone(self.store.claim_event())
        active = self.task("task-key-0002")
        event, _ = self.store.claim_event()
        self.store.complete(active["id"], {}, "complete-key-0002")
        self.store.finish_event(event["id"], {"thread_id": "late-receipt", "adapter": "demo"})
        self.assertEqual(self.store.get_task(active["id"])["status"], "completed")

    def test_question_published_inside_callback_survives_start_receipt(self):
        task = self.task()
        event, _ = self.store.claim_event()
        self.store.publish_questions(task["id"], [{"id": "quantity", "title": "How many?"}], "questions-0001")
        self.store.finish_event(event["id"], {"thread_id": "same-thread", "adapter": "demo"})
        self.assertEqual(self.store.get_task(task["id"])["status"], "needs_input")

    def test_validation_rejects_non_finite_data_and_unsafe_file_paths(self):
        self.assert_error("invalid_input", self.store.create_task, "Title", {"value": float("nan")}, [], "create-key-0001")
        file = {"name": "../outside.txt", "storage_name": "valid-name", "sha256": "0" * 64, "size": 2}
        self.assert_error("invalid_input", self.store.create_task, "Title", {}, [file], "create-key-0001")
        self.assert_error("invalid_input", self.store.get_task, "../outside")

    def test_concurrent_claims_do_not_duplicate_start(self):
        self.task()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.store.claim_event(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_start_receipt_requires_complete_adapter_identity(self):
        task = self.task()
        event, _ = self.store.claim_event()
        invalid = [
            {"thread_id": "existing-thread"},
            {"thread_id": "existing-thread", "adapter": "custom"},
            {"thread_id": "existing-thread", "adapter": "webhook"},
            {"thread_id": "existing-thread", "adapter": "webhook", "callback_peer": "not-a-sha"},
            {"thread_id": "existing-thread", "adapter": "codex-desktop"},
            {"thread_id": "existing-thread", "adapter": "codex-desktop", "project_id": "example",
             "project_path": "relative/path", "host_id": "local"},
            {"thread_id": "existing-thread", "adapter": "codex-desktop", "project_id": "example",
             "project_path": self.tmp.name, "host_id": "remote"},
        ]
        for receipt in invalid:
            self.assert_error("invalid_receipt", self.store.finish_event, event["id"], receipt)
            self.assertIsNone(self.store.get_task(task["id"])["thread"])

    def test_reply_receipt_cannot_switch_adapter_or_callback_peer(self):
        task = self.task()
        event, _ = self.store.claim_event()
        receipt = {"thread_id": "same-thread", "adapter": "webhook", "callback_peer": "a" * 64}
        self.store.finish_event(event["id"], receipt)
        asked = self.store.publish_questions(task["id"], [{"id": "quantity", "title": "How many?"}], "questions-0001")
        self.reply(asked)
        event, _ = self.store.claim_event()
        self.assert_error("thread_mismatch", self.store.finish_event, event["id"],
                          {**receipt, "callback_peer": "b" * 64})
        self.assert_error("thread_mismatch", self.store.finish_event, event["id"],
                          {"thread_id": "same-thread", "adapter": "demo"})
        self.store.finish_event(event["id"], receipt)

    def test_desktop_reply_receipt_preserves_exact_host_and_project(self):
        task = self.task()
        event, _ = self.store.claim_event()
        receipt = {"thread_id": "same-thread", "adapter": "codex-desktop", "host_id": "local",
                   "project_id": "example-project", "project_path": self.tmp.name}
        self.store.finish_event(event["id"], receipt)
        asked = self.store.publish_questions(task["id"], [{"id": "quantity", "title": "How many?"}], "questions-0001")
        self.reply(asked)
        event, _ = self.store.claim_event()
        for changed in ({**receipt, "project_id": "other-project"},
                        {**receipt, "project_path": str(Path(self.tmp.name) / "other-project")},
                        {key: value for key, value in receipt.items() if key != "project_id"}):
            self.assert_error("thread_mismatch" if "project_id" in changed else "invalid_receipt",
                              self.store.finish_event, event["id"], changed)
        self.store.finish_event(event["id"], receipt)

    def test_manual_resolution_requires_adapter_and_preserves_original_peer(self):
        task = self.task()
        event, _ = self.store.claim_event()
        self.store.fail_event(event["id"], "No confirmed receipt", uncertain=True)
        self.assert_error("invalid_receipt", self.store.resolve_event, task["id"], event["id"],
                          {"thread_id": "verified-thread"})
        receipt = {"thread_id": "verified-thread", "adapter": "webhook", "callback_peer": "c" * 64}
        self.store.resolve_event(task["id"], event["id"], receipt)
        asked = self.store.publish_questions(task["id"], [{"id": "quantity", "title": "How many?"}], "questions-0001")
        self.reply(asked)
        event, _ = self.store.claim_event()
        self.store.fail_event(event["id"], "No confirmed receipt", uncertain=True)
        self.assert_error("thread_mismatch", self.store.resolve_event, task["id"], event["id"],
                          {**receipt, "callback_peer": "d" * 64})
        self.store.resolve_event(task["id"], event["id"], receipt)


if __name__ == "__main__":
    unittest.main()
