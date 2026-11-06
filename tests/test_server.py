from concurrent.futures import ThreadPoolExecutor
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
import subprocess
import sys
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from dialogue_bridge.config import DEFAULTS
from dialogue_bridge.errors import BridgeError
from dialogue_bridge.server import Application, BridgeHTTPServer
from dialogue_bridge.adapters import make_adapter


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temporary.name), dict(DEFAULTS))
        self.server = BridgeHTTPServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, path, body=None, role="browser", headers=None):
        actual = {"Content-Type": "application/json"}
        if role:
            actual["Authorization"] = "Bearer " + self.app.credentials[role + "_token"]
        actual.update(headers or {})
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                          headers=actual, method="POST" if body is not None else "GET")
        try:
            response = build_opener(ProxyHandler({})).open(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            content = json.loads(raw) if response.headers.get_content_type() == "application/json" else raw
            return response.code, content, response.headers

    def upload(self, key="upload-request-001", data=None):
        return self.request("/api/tasks", {
            "title": "通用资料处理", "data": data or {"message": "整理为摘要"},
            "files": [{"name": "资料.txt", "content_base64": base64.b64encode("示例数据".encode()).decode()}],
            "request_key": key,
        })[1]["task"]

    def test_demo_upload_web_reply_and_original_dialogue_completion(self):
        task = self.upload()
        self.assertEqual(task["events"][0]["type"], "dialogue.start")
        self.app.dispatcher.once()
        task = self.request("/api/tasks/" + task["id"])[1]["task"]
        original = task["thread"]["thread_id"]
        self.assertEqual(task["status"], "needs_input")
        code, body, _ = self.request("/api/tasks/" + task["id"] + "/reply", {
            "revision": task["question_group"]["revision"], "request_key": "reply-request-001",
            "answers": [{"question_id": "details", "text": "请整理摘要"}],
        })
        self.assertEqual(code, 200)
        self.assertEqual(body["task"]["question_group"]["status"], "answered")
        self.app.dispatcher.once()
        completed = self.app.store.get_task(task["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertIsNone(completed["question_group"])
        self.assertEqual(completed["thread"]["thread_id"], original)
        self.assertTrue(all(event["status"] == "delivered" for event in completed["events"]))

    def test_roles_are_separate_and_config_contains_no_credentials(self):
        self.assertEqual(self.request("/api/tasks", role=None)[0], 401)
        self.assertEqual(self.request("/api/tasks", role="worker")[0], 401)
        self.assertEqual(self.request("/api/worker/tasks", role="browser")[0], 401)
        config = self.request("/api/config")[1]
        self.assertEqual(config["adapter"], "demo")
        for token in self.app.credentials.values():
            self.assertNotIn(token, json.dumps(config))

    def test_static_assets_and_security_headers(self):
        for path, contains in [("/", b"/static/app.js"), ("/static/app.js", b"Authorization"),
                               ("/static/styles.css", b"body")]:
            code, raw, headers = self.request(path, role=None)
            self.assertEqual(code, 200)
            self.assertIn(contains, raw)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_wrong_host_origin_and_query_token_are_rejected(self):
        self.assertEqual(self.request("/api/tasks", headers={"Host": "attacker.example:8765"})[0], 403)
        self.assertEqual(self.request("/api/tasks", headers={"Origin": "https://attacker.example"})[0], 403)
        self.assertEqual(self.request("/api/tasks?token=unused")[0], 400)
        self.assertEqual(self.request("/api/%2e%2e/tasks")[0], 400)

    def test_files_download_with_browser_and_worker_credentials_and_task_scope(self):
        task = self.upload()
        file = task["files"][0]
        for prefix, role in [("/api/", "browser"), ("/api/worker/", "worker")]:
            code, raw, headers = self.request(prefix + "tasks/" + task["id"] + "/files/" + file["storage_name"], role=role)
            self.assertEqual(code, 200)
            self.assertEqual(raw, "示例数据".encode())
            self.assertIn("attachment", headers["Content-Disposition"])
        other = self.upload("upload-request-002")
        self.assertEqual(self.request("/api/tasks/" + other["id"] + "/files/" + file["storage_name"])[0], 404)
        browser = self.request("/api/tasks/" + task["id"])[1]["task"]
        self.assertNotIn("local_path", browser["files"][0])
        worker = self.request("/api/worker/tasks/" + task["id"], role="worker")[1]["task"]
        self.assertTrue(Path(worker["files"][0]["local_path"]).is_file())
        self.assertTrue(worker["files"][0]["download_path"].startswith("/api/worker/"))

    def test_parallel_upload_retry_returns_one_task_and_complete_blob(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            tasks = list(executor.map(lambda _: self.upload(), range(8)))
        self.assertEqual(len({task["id"] for task in tasks}), 1)
        self.assertEqual(len(self.app.store.list_tasks()), 1)
        self.assertEqual(len(list((self.app.data_dir / "uploads").iterdir())), 1)

    def test_interrupted_upload_leaves_no_final_partial_blob(self):
        body = {"title": "upload", "data": {}, "request_key": "interruption-001",
                "files": [{"name": "sample.txt", "content_base64": "YWJj"}]}
        with patch("dialogue_bridge.server.os.replace", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                self.app.create_task(body)
        self.assertEqual(list((self.app.data_dir / "uploads").iterdir()), [])
        task = self.app.create_task(body)
        self.assertEqual(self.app.file_path(task, task["files"][0]["storage_name"]).read_bytes(), b"abc")

    def test_upload_filename_size_and_corrupted_content_fail_closed(self):
        body = {"title": "upload", "data": {}, "request_key": "bad-filename-001",
                "files": [{"name": "../secret.txt", "content_base64": "YWJj"}]}
        self.assertEqual(self.request("/api/tasks", body)[0], 400)
        self.app.config["max_file_bytes"] = 2
        body["files"][0]["name"] = "sample.txt"
        self.assertEqual(self.request("/api/tasks", body)[0], 413)
        self.app.config["max_file_bytes"] = DEFAULTS["max_file_bytes"]
        task = self.upload()
        self.app.file_path(task, task["files"][0]["storage_name"]).write_bytes(b"changed")
        self.assertEqual(self.request("/api/tasks/" + task["id"] + "/files/" + task["files"][0]["storage_name"])[0], 409)

    def test_worker_publish_complete_and_browser_cannot_simulate_worker(self):
        task = self.upload()
        body = {"questions": [{"id": "format", "title": "需要哪种格式？"}], "request_key": "worker-question-001"}
        path = "/api/worker/tasks/" + task["id"] + "/questions"
        self.assertEqual(self.request(path, body, role="browser")[0], 401)
        self.assertEqual(self.request(path, body, role="worker")[0], 200)
        self.assertEqual(self.request("/api/worker/tasks/" + task["id"] + "/complete", {
            "result": {"summary": "done"}, "request_key": "worker-complete-001"}, role="worker")[0], 200)
        self.assertEqual(self.app.store.get_task(task["id"])["status"], "completed")

    def test_stale_reply_and_unknown_retry_fail(self):
        task = self.upload()
        self.app.dispatcher.once()
        body = {"revision": 999, "answers": [{"question_id": "details", "text": "old"}], "request_key": "stale-request-001"}
        self.assertEqual(self.request("/api/tasks/" + task["id"] + "/reply", body)[0], 409)
        newer = self.upload("upload-new-task-001")
        event, _ = self.app.store.claim_event()
        self.app.store.fail_event(event["id"], "result unknown", uncertain=True)
        self.assertEqual(self.request("/api/tasks/" + newer["id"] + "/events/" + event["id"] + "/retry", {})[0], 409)

    def test_cli_can_reask_identical_questions_in_a_new_round_and_retry_that_round(self):
        from dialogue_bridge.config import write_json
        write_json(self.app.data_dir / "runtime.json", {"port": self.server.server_port, "host": "127.0.0.1"})
        task = self.upload()
        event, _ = self.app.store.claim_event()
        self.app.store.finish_event(event["id"], {"adapter": "demo", "thread_id": "demo-thread"})
        questions_file = self.app.data_dir / "questions.json"
        write_json(questions_file, {"questions": [{"id": "details", "title": "请选择输出形式。"}]})
        command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve().parents[1] / "bridge.py"),
                   "--data-dir", str(self.app.data_dir), "publish-questions", "--task-id", task["id"],
                   "--file", str(questions_file)]
        def publish():
            result = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)["task"]
        initial = publish()
        self.assertEqual(initial["question_group"]["revision"], 1)
        self.app.store.reply(task["id"], 1, [{"question_id": "details", "text": "unclear"}], "first-round-answer")
        event, _ = self.app.store.claim_event()
        self.app.store.finish_event(event["id"], {"adapter": "demo", "thread_id": "demo-thread"})
        next_round = publish()
        self.assertEqual(next_round["question_group"]["revision"], 2)
        self.assertEqual(next_round["question_group"]["status"], "pending")
        self.assertEqual(publish()["question_group"]["revision"], 2)

    def test_upload_directory_reparse_is_rejected(self):
        real_lstat = Path.lstat
        directory = self.app.data_dir / "uploads"
        def fake_lstat(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            if path == directory:
                class Reparse:
                    st_file_attributes = 0x400
                return Reparse()
            return result
        with patch.object(Path, "lstat", fake_lstat):
            with self.assertRaises(BridgeError):
                self.app.uploads_directory()

    def test_real_webhook_round_trip_reads_file_asks_then_continues_same_thread(self):
        received, downloaded, failures = [], [], []
        outer = self
        class Receiver(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                try:
                    event = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    received.append(event)
                    outer.assertEqual(self.headers["Authorization"], "Bearer receiver-test-token")
                    outer.assertEqual(self.headers["Idempotency-Key"], event["event_id"])
                    task_id = event["task"]["id"]
                    if event["type"] == "dialogue.start":
                        file = event["task"]["files"][0]
                        outer.assertNotIn("local_path", file)
                        code, content, _ = outer.request(file["download_path"], role="worker")
                        outer.assertEqual(code, 200)
                        downloaded.append(content)
                        code, _, _ = outer.request("/api/worker/tasks/" + task_id + "/questions", {
                            "questions": [{"id": "format", "title": "需要什么格式？"}],
                            "request_key": "receiver-questions-001"}, role="worker")
                        outer.assertEqual(code, 200)
                    else:
                        outer.assertEqual(event["event"]["answers"][0]["text"], "摘要")
                        outer.assertEqual(event["task"]["thread"]["thread_id"], "external-" + task_id)
                        code, _, _ = outer.request("/api/worker/tasks/" + task_id + "/complete", {
                            "result": {"summary": "接收端已处理网页回复"}, "request_key": "receiver-complete-001"},
                            role="worker")
                        outer.assertEqual(code, 200)
                    raw = json.dumps({"accepted": True, "thread_id": "external-" + task_id}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except Exception as exc:
                    failures.append(str(exc))
                    self.send_error(500)
        receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
        receiver_thread.start()
        try:
            self.app.config.update({"adapter": "webhook", "callback_url": "http://127.0.0.1:" + str(receiver.server_port) + "/events",
                                    "callback_token": "receiver-test-token"})
            self.app.adapter = make_adapter(self.app.config, data_dir=self.app.data_dir,
                                            project_dir=self.app.data_dir, cli_path=Path("bridge.py"))
            task = self.upload()
            self.app.dispatcher.once()
            current = self.app.store.get_task(task["id"])
            self.assertEqual(current["status"], "needs_input", failures)
            code, _, _ = self.request("/api/tasks/" + task["id"] + "/reply", {
                "revision": current["question_group"]["revision"], "request_key": "receiver-reply-001",
                "answers": [{"question_id": "format", "text": "摘要"}]})
            self.assertEqual(code, 200)
            self.app.dispatcher.once()
            final = self.app.store.get_task(task["id"])
            self.assertEqual(final["status"], "completed", failures)
            self.assertTrue(all(event["status"] == "delivered" for event in final["events"]), failures)
            self.assertEqual([item["type"] for item in received], ["dialogue.start", "dialogue.reply"])
            self.assertEqual(downloaded, ["示例数据".encode()])
            self.assertEqual(failures, [])
        finally:
            receiver.shutdown()
            receiver.server_close()
            receiver_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
