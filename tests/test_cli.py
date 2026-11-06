import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def test_background_service_upload_reply_complete_and_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            base = [sys.executable, str(ROOT / "bridge.py"), "--data-dir", str(directory)]
            def command(*args):
                result = subprocess.run(base + list(args), cwd=ROOT, capture_output=True,
                                        encoding="utf-8", timeout=25,
                                        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout
            command("init")
            process_started = False
            try:
                command("start", "--port", "0")
                process_started = True
                runtime = json.loads((directory / "runtime.json").read_text())
                tokens = json.loads((directory / "credentials.json").read_text())
                base_url = "http://127.0.0.1:" + str(runtime["port"])
                def request(path, data=None):
                    req = Request(base_url + path, data=json.dumps(data).encode() if data is not None else None,
                                  headers={"Authorization": "Bearer " + tokens["browser_token"],
                                           "Content-Type": "application/json"})
                    with build_opener(ProxyHandler({})).open(req, timeout=5) as response:
                        return json.loads(response.read())
                task = request("/api/tasks", {"title": "Example upload", "data": {"message": "Please summarize"},
                                              "files": [], "request_key": "cli-upload-0001"})["task"]
                until = time.monotonic() + 5
                while time.monotonic() < until:
                    task = request("/api/tasks/" + task["id"])["task"]
                    if task["status"] == "needs_input":
                        break
                    time.sleep(0.1)
                self.assertEqual(task["status"], "needs_input")
                worker_task = json.loads(command("task", "--task-id", task["id"]))["task"]
                self.assertEqual(worker_task["id"], task["id"])
                request("/api/tasks/" + task["id"] + "/reply", {
                    "revision": task["question_group"]["revision"], "request_key": "cli-reply-0001",
                    "answers": [{"question_id": "details", "text": "Summarize"}]})
                until = time.monotonic() + 5
                while time.monotonic() < until:
                    task = request("/api/tasks/" + task["id"])["task"]
                    if task["status"] == "completed":
                        break
                    time.sleep(0.1)
                self.assertEqual(task["status"], "completed")
                self.assertTrue(task["result"]["demo"])
                self.assertIn("#token=", command("url"))
                self.assertIn("127.0.0.1", command("url", "--host", "127.0.0.1"))
            finally:
                if process_started:
                    command("stop")
                    # Give the hidden service time to close its log and SQLite
                    # handles before TemporaryDirectory cleanup on Windows.
                    until = time.monotonic() + 5
                    while time.monotonic() < until:
                        try:
                            request("/api/config")
                        except (URLError, OSError):
                            break
                        time.sleep(0.1)
                    time.sleep(0.2)


if __name__ == "__main__":
    unittest.main()
