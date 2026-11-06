"""Single-service ownership and the durable callback dispatcher."""
from __future__ import annotations

from contextlib import contextmanager
import logging
import os
from pathlib import Path
import threading

from .adapters import DeliveryError
from .errors import BridgeError


@contextmanager
def service_lock(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    stream = (data_dir / "service.lock").open("a+b")
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise BridgeError("already_running", "该数据目录已有服务运行。", 409) from exc
    try:
        yield
    finally:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


class Dispatcher:
    def __init__(self, app):
        self.app = app
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="dialogue-callback", daemon=True)

    def start(self):
        self.app.store.recover_inflight()
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def once(self) -> bool:
        claimed = self.app.store.claim_event()
        if not claimed:
            return False
        event, task = claimed
        try:
            receipt = self.app.adapter.deliver(event, self.app.local_task(task))
            if self.app.config["adapter"] == "demo":
                if event["type"] == "dialogue.start":
                    self.app.store.publish_questions(task["id"], [{
                        "id": "details", "title": "请补充这份数据需要怎样处理？这是演示问题，尚未连接真实 AI。",
                        "options": ["整理为摘要", "检查缺失内容"],
                    }], "demo-question-" + event["id"])
                else:
                    self.app.store.complete(task["id"], {
                        "summary": "演示流程已完成：网页回复已保存，并回调到同一个演示对话。",
                        "answers": event.get("answers", []),
                        "demo": True,
                    }, "demo-complete-" + event["id"])
            self.app.store.finish_event(event["id"], receipt)
        except DeliveryError as exc:
            self.app.store.fail_event(event["id"], str(exc), uncertain=exc.uncertain)
            logging.warning("callback event=%s status=%s", event["id"], "unknown" if exc.uncertain else "failed")
        except Exception:
            # The adapter may already have sent a mutation. Never replay it just
            # because receipt validation or local persistence failed afterward.
            self.app.store.fail_event(event["id"], "回调结果未能核实，请在管理员端检查。", uncertain=True)
            logging.exception("callback event=%s requires reconciliation", event["id"])
        return True

    def run(self):
        while not self.stop.is_set():
            try:
                if self.once():
                    continue
            except Exception:
                logging.exception("callback dispatcher paused after local error")
            self.stop.wait(0.3)
