"""Transactional task, question and callback state, with an explicit outbox.

Replies and callback events commit in one SQLite transaction. A callback whose
result is uncertain is never replayed automatically: an operator must reconcile
the original action and supply its receipt. No business data writer lives here.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
import uuid

from .errors import BridgeError


_KEY = re.compile(r"[A-Za-z0-9_.:-]{8,128}\Z")
_ID = re.compile(r"[a-f0-9]{32}\Z")
_QUESTION_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,79}\Z")
_THREAD_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_STORAGE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,200}\Z")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require(condition: Any, message: str, code: str = "invalid_input", status: int = 400) -> None:
    if not condition:
        raise BridgeError(code, message, status)


def _json(value: Any) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        raw.encode("utf-8")
        return raw
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise BridgeError("invalid_input", "Expected finite JSON data.") from exc


def _object(value: Any, limit: int, label: str) -> dict:
    _require(isinstance(value, dict), f"{label} must be a JSON object.")
    raw = _json(value)
    _require(len(raw.encode("utf-8")) <= limit, f"{label} is too large.")
    return json.loads(raw)


def _text(value: Any, maximum: int, label: str) -> str:
    _require(isinstance(value, str) and 0 < len(value.strip()) <= maximum,
             f"{label} must contain 1–{maximum} characters.")
    _require(not any(ord(c) < 32 and c not in "\r\n\t" for c in value),
             f"{label} contains unsupported control characters.")
    return value.strip()


def _key(value: Any) -> str:
    _require(isinstance(value, str) and _KEY.fullmatch(value),
             "request_key must be 8–128 letters, digits, dots, colons, underscores or hyphens.")
    return value


def _identity(value: Any) -> str:
    _require(isinstance(value, str) and _ID.fullmatch(value), "Invalid task or event ID.")
    return value


def _questions(value: Any) -> list[dict]:
    _require(isinstance(value, list) and 1 <= len(value) <= 10, "Provide 1–10 questions.")
    result, ids = [], set()
    for question in value:
        _require(isinstance(question, dict) and {"id", "title"} <= set(question)
                 <= {"id", "title", "options"}, "Each question needs an ID and title.")
        identifier = question["id"]
        _require(isinstance(identifier, str) and _QUESTION_ID.fullmatch(identifier)
                 and identifier not in ids, "Question IDs must be valid and unique.")
        ids.add(identifier)
        normalized = {"id": identifier, "title": _text(question["title"], 4000, "Question")}
        if "options" in question:
            options = question["options"]
            _require(isinstance(options, list) and 1 <= len(options) <= 10, "Provide 1–10 suggestions.")
            options = [_text(option, 300, "Suggestion") for option in options]
            _require(len(options) == len(set(options)), "Suggestions must be unique.")
            normalized["options"] = options
        result.append(normalized)
    return result


def _answers(value: Any) -> list[dict]:
    _require(isinstance(value, list) and 1 <= len(value) <= 10, "Answer every question.")
    result, ids = [], set()
    for answer in value:
        _require(isinstance(answer, dict) and set(answer) == {"question_id", "text"},
                 "Each answer needs question_id and text.")
        identifier = answer["question_id"]
        _require(isinstance(identifier, str) and _QUESTION_ID.fullmatch(identifier)
                 and identifier not in ids, "Answer IDs must be valid and unique.")
        ids.add(identifier)
        result.append({"question_id": identifier, "text": _text(answer["text"], 12000, "Answer")})
    _require(sum(len(item["text"]) for item in result) <= 24000, "Combined answers exceed 24000 characters.")
    return sorted(result, key=lambda item: item["question_id"])


def _files(value: Any) -> list[dict]:
    _require(isinstance(value, list) and len(value) <= 20, "Provide at most 20 files.")
    result = []
    for item in value:
        _require(isinstance(item, dict) and set(item) == {"name", "storage_name", "sha256", "size"},
                 "Invalid stored-file metadata.")
        name = _text(item["name"], 255, "File name")
        _require(not any(c in name for c in ("/", "\\", "\x00")) and name not in (".", ".."),
                 "File name must not contain a path.")
        _require(isinstance(item["storage_name"], str) and _STORAGE_NAME.fullmatch(item["storage_name"])
                 and item["storage_name"] not in (".", ".."), "Invalid internal file name.")
        _require(isinstance(item["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", item["sha256"]),
                 "Invalid file digest.")
        _require(type(item["size"]) is int and 0 <= item["size"] <= 20 * 1024 * 1024,
                 "Invalid file size.")
        result.append({**item, "name": name})
    return result


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "bridge.sqlite3"
        _require(not self.path.is_symlink(), "State database cannot be a link.")
        with self._transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
            conn.execute("""CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                task_id TEXT NOT NULL REFERENCES tasks(id), status TEXT NOT NULL,
                document TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS events_pending ON events(status, seq)")
            conn.execute("""CREATE TABLE IF NOT EXISTS requests (
                scope TEXT NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                task_id TEXT NOT NULL REFERENCES tasks(id), PRIMARY KEY(scope, request_key))""")

    @contextmanager
    def _transaction(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _task(self, conn, identifier: str) -> dict:
        row = conn.execute("SELECT document FROM tasks WHERE id=?", (_identity(identifier),)).fetchone()
        _require(row is not None, "Task not found.", "not_found", 404)
        return json.loads(row["document"])

    def _snapshot(self, conn, task: dict) -> dict:
        result = json.loads(_json(task))
        result["events"] = [json.loads(row[0]) for row in conn.execute(
            "SELECT document FROM events WHERE task_id=? ORDER BY seq", (task["id"],))]
        return result

    def _save(self, conn, task: dict) -> None:
        task["updated_at"] = _now()
        conn.execute("UPDATE tasks SET document=? WHERE id=?", (_json(task), task["id"]))

    def _event(self, conn, identifier: str) -> dict:
        row = conn.execute("SELECT document FROM events WHERE id=?", (_identity(identifier),)).fetchone()
        _require(row is not None, "Callback event not found.", "not_found", 404)
        return json.loads(row[0])

    def _save_event(self, conn, event: dict) -> None:
        event["updated_at"] = _now()
        conn.execute("UPDATE events SET status=?,document=? WHERE id=?",
                     (event["status"], _json(event), event["id"]))

    def _new_event(self, conn, task: dict, kind: str, **payload) -> dict:
        event = {"id": uuid.uuid4().hex, "task_id": task["id"], "type": kind,
                 "status": "pending", "created_at": _now(), "updated_at": _now(),
                 "attempts": 0, "receipt": None, "error": None, **payload}
        conn.execute("INSERT INTO events(id,task_id,status,document) VALUES(?,?,?,?)",
                     (event["id"], task["id"], event["status"], _json(event)))
        return event

    def _request(self, conn, scope: str, request_key: str, payload: Any) -> str | None:
        _key(request_key)
        fingerprint = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
        row = conn.execute("SELECT fingerprint,task_id FROM requests WHERE scope=? AND request_key=?",
                           (scope, request_key)).fetchone()
        if row:
            _require(row["fingerprint"] == fingerprint,
                     "This request key was already used for different content.", "idempotency_conflict", 409)
            return row["task_id"]
        return None

    def _remember(self, conn, scope: str, request_key: str, payload: Any, task_id: str) -> None:
        conn.execute("INSERT INTO requests(scope,request_key,fingerprint,task_id) VALUES(?,?,?,?)",
                     (scope, request_key, hashlib.sha256(_json(payload).encode("utf-8")).hexdigest(), task_id))

    def create_task(self, title: str, data: dict, files: list, request_key: str) -> dict:
        title = _text(title, 200, "Title")
        data = _object(data, 1024 * 1024, "Data")
        files = _files(files)
        # Internal blob names are not part of the user's submission identity.
        payload = {"title": title, "data": data,
                   "files": [{k: v for k, v in item.items() if k != "storage_name"} for item in files]}
        with self._transaction() as conn:
            existing = self._request(conn, "create", request_key, payload)
            if existing:
                return self._snapshot(conn, self._task(conn, existing))
            now = _now()
            task = {"id": uuid.uuid4().hex, "title": title, "data": data, "files": files,
                    "status": "queued", "thread": None, "question_group": None, "revision": 0,
                    "history": [], "result": None, "created_at": now, "updated_at": now}
            conn.execute("INSERT INTO tasks(id,document) VALUES(?,?)", (task["id"], _json(task)))
            self._new_event(conn, task, "dialogue.start")
            self._remember(conn, "create", request_key, payload, task["id"])
            return self._snapshot(conn, task)

    def list_tasks(self) -> list[dict]:
        with self._transaction() as conn:
            tasks = [json.loads(row[0]) for row in conn.execute("SELECT document FROM tasks ORDER BY rowid DESC")]
            return [self._snapshot(conn, task) for task in tasks]

    def get_task(self, task_id: str) -> dict:
        with self._transaction() as conn:
            return self._snapshot(conn, self._task(conn, task_id))

    def _live(self, task: dict) -> None:
        _require(task["status"] != "completed", "Task is already completed.", "task_completed", 409)

    def _archive_group(self, task: dict) -> None:
        group = task.get("question_group")
        if group:
            group = json.loads(_json(group))
            if group["status"] == "pending":
                group["status"] = "superseded"
            task["history"].append(group)

    def publish_questions(self, task_id: str, questions: list, request_key: str) -> dict:
        questions = _questions(questions)
        payload = {"questions": questions}
        scope = f"{_identity(task_id)}:questions"
        with self._transaction() as conn:
            task = self._task(conn, task_id)
            if self._request(conn, scope, request_key, payload):
                return self._snapshot(conn, task)
            self._live(task)
            blocked = conn.execute("SELECT document FROM events WHERE task_id=? AND status != 'delivered'",
                                   (task_id,)).fetchall()
            _require(not any(json.loads(row[0])["type"] == "dialogue.reply" for row in blocked),
                     "The preceding reply has not been delivered yet.", "reply_not_delivered", 409)
            group = task["question_group"]
            if not (group and group["status"] == "pending" and group["questions"] == questions):
                self._archive_group(task)
                task["revision"] += 1
                task["question_group"] = {"revision": task["revision"], "questions": questions,
                                          "status": "pending", "created_at": _now()}
                task["status"] = "needs_input"
                self._save(conn, task)
            self._remember(conn, scope, request_key, payload, task_id)
            return self._snapshot(conn, task)

    def reply(self, task_id: str, revision: int, answers: list, request_key: str) -> dict:
        _require(type(revision) is int and revision > 0, "Invalid question revision.")
        answers = _answers(answers)
        payload = {"revision": revision, "answers": answers}
        scope = f"{_identity(task_id)}:reply"
        with self._transaction() as conn:
            task = self._task(conn, task_id)
            if self._request(conn, scope, request_key, payload):
                return self._snapshot(conn, task)
            self._live(task)
            group = task["question_group"]
            _require(group and group["status"] == "pending" and group["revision"] == revision,
                     "These questions changed or were already answered. Refresh the task.", "stale_questions", 409)
            _require({a["question_id"] for a in answers} == {q["id"] for q in group["questions"]},
                     "Answers must match every current question exactly.")
            event = self._new_event(conn, task, "dialogue.reply", revision=revision,
                                    questions=group["questions"], answers=answers)
            group.update(status="answered", answers=answers, event_id=event["id"], answered_at=_now())
            task["status"] = "running"
            self._save(conn, task)
            self._remember(conn, scope, request_key, payload, task_id)
            return self._snapshot(conn, task)

    def complete(self, task_id: str, result: dict, request_key: str) -> dict:
        result = _object(result, 1024 * 1024, "Result")
        scope = f"{_identity(task_id)}:complete"
        with self._transaction() as conn:
            task = self._task(conn, task_id)
            if self._request(conn, scope, request_key, result):
                return self._snapshot(conn, task)
            self._live(task)
            self._archive_group(task)
            task.update(status="completed", question_group=None, result=result, completed_at=_now())
            for row in conn.execute("SELECT document FROM events WHERE task_id=? AND status IN ('pending','failed')",
                                    (task_id,)).fetchall():
                event = json.loads(row[0])
                event["status"] = "cancelled"
                self._save_event(conn, event)
            self._save(conn, task)
            self._remember(conn, scope, request_key, result, task_id)
            return self._snapshot(conn, task)

    def claim_event(self) -> tuple[dict, dict] | None:
        with self._transaction() as conn:
            for row in conn.execute("SELECT document FROM events WHERE status='pending' ORDER BY seq").fetchall():
                event = json.loads(row[0])
                task = self._task(conn, event["task_id"])
                if task["status"] == "completed":
                    continue
                if conn.execute("SELECT 1 FROM events WHERE task_id=? AND status='sending'", (task["id"],)).fetchone():
                    continue
                if event["type"] == "dialogue.reply" and not task["thread"]:
                    continue
                event.update(status="sending", attempts=event["attempts"] + 1)
                self._save_event(conn, event)
                if task["status"] == "queued":
                    task["status"] = "running"
                    self._save(conn, task)
                return event, self._snapshot(conn, task)
            return None

    def _finish(self, conn, task: dict, event: dict, receipt: dict, *, manual: bool = False) -> None:
        receipt = _object(receipt, 65536, "Callback receipt")
        thread_id = receipt.get("thread_id")
        _require(isinstance(thread_id, str) and _THREAD_ID.fullmatch(thread_id),
                 "Callback receipt needs a valid thread_id.", "invalid_receipt", 409)
        adapter = receipt.get("adapter")
        _require(adapter in ("demo", "webhook", "codex-desktop"),
                 "Callback receipt needs a supported adapter identity.", "invalid_receipt", 409)
        if adapter == "webhook":
            peer = receipt.get("callback_peer")
            _require(isinstance(peer, str) and re.fullmatch(r"[a-f0-9]{64}", peer),
                     "Webhook receipt needs the exact callback peer fingerprint.", "invalid_receipt", 409)
        elif adapter == "codex-desktop":
            project_id, project_path = receipt.get("project_id"), receipt.get("project_path")
            _require(isinstance(project_id, str) and _THREAD_ID.fullmatch(project_id)
                     and isinstance(project_path, str) and "\x00" not in project_path
                     and Path(project_path).is_absolute() and receipt.get("host_id") == "local",
                     "Desktop receipt needs a local host and absolute project identity.", "invalid_receipt", 409)
        stored = task["thread"]
        if stored:
            _require(stored["thread_id"] == thread_id,
                     "Callback returned a different conversation.", "thread_mismatch", 409)
            for key in ("adapter", "callback_peer", "project_id", "project_path", "host_id"):
                _require(key not in stored or stored[key] == receipt.get(key),
                         "Callback returned a different project or host.", "thread_mismatch", 409)
        else:
            _require(event["type"] == "dialogue.start", "Cannot reply before a conversation starts.",
                     "invalid_receipt", 409)
            task["thread"] = receipt
        event.update(status="delivered", receipt=receipt, error=None, delivered_at=_now())
        if manual:
            event["resolved_manually_at"] = _now()
        self._save_event(conn, event)
        self._save(conn, task)

    def finish_event(self, event_id: str, receipt: dict) -> None:
        with self._transaction() as conn:
            event = self._event(conn, event_id)
            _require(event["status"] == "sending", "Callback event is no longer sending.", "event_state", 409)
            self._finish(conn, self._task(conn, event["task_id"]), event, receipt)

    def fail_event(self, event_id: str, message: str, uncertain: bool = False) -> None:
        message = _text(message, 2000, "Callback error")
        _require(type(uncertain) is bool, "Invalid callback certainty.")
        with self._transaction() as conn:
            event = self._event(conn, event_id)
            _require(event["status"] == "sending", "Callback event is no longer sending.", "event_state", 409)
            event.update(status="unknown" if uncertain else "failed", error=message)
            self._save_event(conn, event)

    def retry_event(self, task_id: str, event_id: str) -> dict:
        with self._transaction() as conn:
            task, event = self._task(conn, task_id), self._event(conn, event_id)
            _require(event["task_id"] == task_id, "Callback event belongs to another task.", "not_found", 404)
            self._live(task)
            _require(event["status"] == "failed", "Only a confirmed failed callback can be retried.", "event_state", 409)
            event.update(status="pending", error=None)
            self._save_event(conn, event)
            return self._snapshot(conn, task)

    def recover_inflight(self) -> None:
        with self._transaction() as conn:
            for row in conn.execute("SELECT document FROM events WHERE status='sending'").fetchall():
                event = json.loads(row[0])
                event.update(status="unknown", error="Server restarted during delivery; reconcile the original action before continuing.")
                self._save_event(conn, event)

    def resolve_event(self, task_id: str, event_id: str, receipt: dict) -> dict:
        with self._transaction() as conn:
            task, event = self._task(conn, task_id), self._event(conn, event_id)
            _require(event["task_id"] == task_id, "Callback event belongs to another task.", "not_found", 404)
            _require(event["status"] == "unknown", "Only an uncertain callback can be reconciled.", "event_state", 409)
            self._finish(conn, task, event, receipt, manual=True)
            return self._snapshot(conn, task)
