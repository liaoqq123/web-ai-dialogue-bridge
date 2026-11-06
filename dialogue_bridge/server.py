"""Same-origin web API, uploads, and a separate authenticated worker API."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import socket
import stat
import threading
import uuid
from urllib.parse import quote, urlsplit

from . import __version__
from .adapters import make_adapter
from .config import credentials
from .errors import BridgeError
from .runtime import Dispatcher
from .store import Store


WEB = Path(__file__).parent / "web"
IDENTIFIER = r"[a-f0-9]{32}"
REQUEST_KEY = re.compile(r"[A-Za-z0-9_.:-]{8,128}")


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def require(value, message, code="input", status=400):
    if not value:
        raise BridgeError(code, message, status)


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "JSON 字段不能重复。")
        result[key] = value
    return result


def parse_json(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=strict_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BridgeError("input", "请求必须为有效 JSON。") from exc
    require(isinstance(value, dict), "JSON 请求必须是对象。")
    return value


class Application:
    def __init__(self, data_dir: Path, config: dict, *, cli_path: Path | None = None):
        self.data_dir = data_dir.resolve()
        self.config = config
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "uploads").mkdir(exist_ok=True)
        self.upload_lock = threading.Lock()
        self.uploads_directory()
        self.credentials = credentials(self.data_dir)
        self.store = Store(self.data_dir)
        self.adapter = make_adapter(config, data_dir=self.data_dir,
                                    project_dir=Path(config.get("project_dir", Path.cwd())).resolve(),
                                    cli_path=cli_path or Path(__file__).resolve().parents[1] / "bridge.py")
        self.dispatcher = Dispatcher(self)

    def local_task(self, task: dict) -> dict:
        # Only trusted local CLI/adapter consumers see server filesystem paths.
        result = json.loads(json.dumps(task, ensure_ascii=False))
        for entry in result.get("files", []):
            entry["local_path"] = str(self.file_path(task, entry["storage_name"]))
            entry["download_path"] = "/api/worker/tasks/" + task["id"] + "/files/" + entry["storage_name"]
        return result

    def uploads_directory(self) -> Path:
        path = self.data_dir / "uploads"
        attributes = path.lstat()
        reparse = getattr(attributes, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        require(path.is_dir() and not path.is_symlink() and not reparse
                and self.data_dir.resolve() == self.data_dir and path.resolve() == path,
                "上传目录不能是链接或越界目录。")
        return path

    def file_path(self, task: dict, storage_name: str) -> Path:
        require(re.fullmatch(IDENTIFIER, storage_name) is not None, "文件编号无效。", status=404)
        require(any(file["storage_name"] == storage_name for file in task["files"]),
                "该文件不属于当前任务。", status=404)
        directory = self.uploads_directory()
        path = directory / storage_name
        require(not path.is_symlink() and path.is_file()
                and not (getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
                and path.resolve().parent == directory,
                "上传文件不存在。", status=404)
        return path

    def create_task(self, body: dict) -> dict:
        require(set(body) == {"title", "data", "files", "request_key"}, "上传参数不完整。")
        require(isinstance(body["title"], str) and 0 < len(body["title"].strip()) <= 200,
                "请输入 1 至 200 字的任务标题。")
        require(isinstance(body["data"], dict) and len(encoded(body["data"])) <= 256 * 1024,
                "任务数据须为不超过 256 KiB 的 JSON 对象。")
        require(isinstance(body["request_key"], str) and REQUEST_KEY.fullmatch(body["request_key"]),
                "上传请求编号无效。")
        raw_files = body["files"]
        require(isinstance(raw_files, list) and len(raw_files) <= self.config["max_files"], "上传文件过多。")
        metadata, prepared, total = [], [], 0
        for index, item in enumerate(raw_files):
            require(isinstance(item, dict) and set(item) == {"name", "content_base64"}, "文件参数无效。")
            name = item["name"]
            require(isinstance(name, str) and 0 < len(name.strip()) <= 200
                    and name not in {".", ".."} and not any(ord(c) < 32 or c in "/\\" for c in name),
                    "文件名称无效。")
            require(isinstance(item["content_base64"], str), "文件必须使用 Base64 编码。")
            require(len(item["content_base64"]) <= (self.config["max_file_bytes"] + 2) // 3 * 4,
                    "单个文件超过上传限制。", status=413)
            try:
                content = base64.b64decode(item["content_base64"], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise BridgeError("input", "文件 Base64 编码无效。") from exc
            total += len(content)
            require(len(content) <= self.config["max_file_bytes"] and total <= self.config["max_total_bytes"],
                    "文件超过上传大小限制。", status=413)
            digest = hashlib.sha256(content).hexdigest()
            # Stable metadata lets a lost upload response be retried with the
            # same key. Neither original filenames nor caller paths reach disk.
            storage = hashlib.sha256(encoded([body["request_key"], index, name, digest])).hexdigest()[:32]
            metadata.append({"name": name, "storage_name": storage, "sha256": digest, "size": len(content)})
            prepared.append((self.data_dir / "uploads" / storage, content))
        require(bool(raw_files) or bool(body["data"]), "请上传文件或提供任务数据。")
        # Files are durable before the task/outbox become visible to the worker.
        # A process crash here may leave an unreferenced blob, never a partial
        # input that an AI can mistake for a successful upload.
        with self.upload_lock:
            self.uploads_directory()
            for path, content in prepared:
                require(not path.is_symlink(), "文件存储路径无效。")
                if path.exists():
                    require(path.is_file() and path.read_bytes() == content, "已存在的上传文件指纹不匹配。", status=409)
                    continue
                temporary = path.with_name("upload-" + uuid.uuid4().hex + ".tmp")
                try:
                    with temporary.open("xb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
        return self.store.create_task(body["title"], body["data"], metadata, body["request_key"])


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app: Application):
        self.app = app
        super().__init__(address, Handler)
        configured = app.config.get("allowed_hosts", [])
        self.allowed_hosts = {"localhost", "127.0.0.1", socket.gethostname().lower(), *configured}
        host = address[0]
        if host == "0.0.0.0":
            # Resolve this server's own local addresses once; never resolve an
            # untrusted Host header. Additional reverse proxy names are explicit.
            try:
                self.allowed_hosts.update(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
            except OSError:
                pass
        else:
            self.allowed_hosts.add(host.lower())


class Handler(BaseHTTPRequestHandler):
    server_version = "DialogueBridge/" + __version__
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)
        self._body_consumed = False

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        # Log neither full URLs nor headers; query strings may contain secrets
        # supplied accidentally even though the application rejects them.
        logging.info("http method=%s status=%s", self.command, args[1] if len(args) > 1 else "-")

    def respond(self, status: int, raw: bytes, content_type="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def json(self, status, body):
        self.respond(status, encoded(body))

    def check_request(self):
        hosts = self.headers.get_all("Host", [])
        require(len(hosts) == 1, "Host 无效。", status=403)
        try:
            host = urlsplit("//" + hosts[0])
            port = host.port or 80
            require(host.username is None and host.password is None and not host.path and not host.query
                    and not host.fragment and host.hostname in self.server.allowed_hosts
                    and port == self.server.server_port, "Host 不在允许列表。", status=403)
        except ValueError as exc:
            raise BridgeError("host", "Host 无效。", 403) from exc
        origins = self.headers.get_all("Origin", [])
        require(len(origins) <= 1, "Origin 无效。", status=403)
        if origins:
            require(origins[0] == "http://" + hosts[0], "不接受跨来源请求。", status=403)
        parsed = urlsplit(self.path)
        require(not parsed.scheme and not parsed.netloc and not parsed.query and not parsed.fragment,
                "请求地址无效；访问凭据请放在网址 #token= 中。")
        require("%" not in parsed.path, "请求路径无效。")
        return parsed.path

    def auth(self, role):
        values = self.headers.get_all("Authorization", [])
        token = self.app.credentials[role + "_token"]
        require(len(values) == 1 and values[0].isascii() and hmac.compare_digest(values[0], "Bearer " + token),
                "访问凭据无效。", "unauthorized", 401)

    def body(self):
        require(not self.headers.get_all("Transfer-Encoding"), "不接受分块请求。")
        lengths = self.headers.get_all("Content-Length", [])
        require(len(lengths) == 1 and lengths[0].isdigit(), "请求长度无效。")
        size = int(lengths[0])
        maximum = self.app.config["max_total_bytes"] * 4 // 3 + 2 * 1024 * 1024
        require(0 < size <= maximum, "请求超过大小限制。", status=413)
        require(self.headers.get_content_type() == "application/json", "请提交 application/json。", status=415)
        self.connection.settimeout(15)
        self._body_consumed = True
        raw = self.rfile.read(size)
        require(len(raw) == size, "请求内容不完整。")
        return parse_json(raw)

    def _handle(self):
        path = self.check_request()
        if self.command in {"GET", "HEAD"} and path in {"/", "/static/app.js", "/static/styles.css"}:
            name = "index.html" if path == "/" else path.rsplit("/", 1)[-1]
            content_type = {"index.html": "text/html", "app.js": "text/javascript", "styles.css": "text/css"}[name]
            self.respond(200, (WEB / name).read_bytes(), content_type + "; charset=utf-8")
            return
        worker = path.startswith("/api/worker/")
        self.auth("worker" if worker else "browser")
        if self.command in {"GET", "HEAD"}:
            if path == "/api/config":
                self.json(200, {"adapter": self.app.config["adapter"], "version": __version__, **{
                    key: self.app.config[key] for key in ("max_file_bytes", "max_total_bytes", "max_files")}})
                return
            if path in {"/api/tasks", "/api/worker/tasks"}:
                tasks = self.app.store.list_tasks()
                self.json(200, {"tasks": [self.app.local_task(task) for task in tasks] if worker else tasks})
                return
            match = re.fullmatch(r"/api/(?:worker/)?tasks/(" + IDENTIFIER + r")", path)
            if match:
                task = self.app.store.get_task(match[1])
                self.json(200, {"task": self.app.local_task(task) if worker else task})
                return
            match = re.fullmatch(r"/api/(?:worker/)?tasks/(" + IDENTIFIER + r")/files/(" + IDENTIFIER + r")", path)
            if match:
                task = self.app.store.get_task(match[1])
                content = self.app.file_path(task, match[2]).read_bytes()
                entry = next(file for file in task["files"] if file["storage_name"] == match[2])
                require(hashlib.sha256(content).hexdigest() == entry["sha256"], "上传文件指纹已变化。", status=409)
                self.respond(200, content, "application/octet-stream", {
                    "Content-Disposition": "attachment; filename=\"download\"; filename*=UTF-8''" + quote(entry["name"], safe="")})
                return
            raise BridgeError("not_found", "接口不存在。", 404)
        require(self.command == "POST", "不支持该请求方式。", status=405)
        body = self.body()
        if path == "/api/tasks":
            self.json(201, {"task": self.app.create_task(body)})
            return
        match = re.fullmatch(r"/api/tasks/(" + IDENTIFIER + r")/reply", path)
        if match:
            require(set(body) == {"revision", "answers", "request_key"}, "回复参数无效。")
            self.json(200, {"task": self.app.store.reply(match[1], **body)})
            return
        match = re.fullmatch(r"/api/tasks/(" + IDENTIFIER + r")/events/(" + IDENTIFIER + r")/retry", path)
        if match:
            require(body == {}, "重试参数无效。")
            self.json(200, {"task": self.app.store.retry_event(match[1], match[2])})
            return
        match = re.fullmatch(r"/api/worker/tasks/(" + IDENTIFIER + r")/(questions|complete)", path)
        if match:
            argument = "questions" if match[2] == "questions" else "result"
            require(set(body) == {argument, "request_key"}, "工作端参数无效。")
            method = self.app.store.publish_questions if match[2] == "questions" else self.app.store.complete
            self.json(200, {"task": method(match[1], body[argument], body["request_key"])})
            return
        match = re.fullmatch(r"/api/worker/tasks/(" + IDENTIFIER + r")/events/(" + IDENTIFIER + r")/resolve", path)
        if match:
            require(set(body) == {"receipt"}, "核对参数无效。")
            self.json(200, {"task": self.app.store.resolve_event(match[1], match[2], body["receipt"])})
            return
        if path == "/api/worker/shutdown":
            require(body == {}, "关闭参数无效。")
            require(ipaddress.ip_address(self.client_address[0]).is_loopback
                    or self.client_address[0] == self.server.server_address[0], "仅本机可关闭服务。", status=403)
            self.json(200, {"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        raise BridgeError("not_found", "接口不存在。", 404)

    def dispatch(self):
        try:
            self._handle()
        except BridgeError as exc:
            # A short rejected POST must be drained before closing its socket.
            # Otherwise Windows may reset the connection while the browser is
            # still writing, hiding the intended 401/409 response entirely.
            if not self._body_consumed and self.command == "POST":
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) == 1 and lengths[0].isdigit() and int(lengths[0]) <= 1024 * 1024:
                    try:
                        self.connection.settimeout(1)
                        self.rfile.read(int(lengths[0]))
                    except OSError:
                        pass
            self.json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (TimeoutError, OSError):
            logging.warning("http local IO failed")
            try:
                self.json(500, {"error": {"code": "local_io", "message": "本地读写失败，请检查服务日志。"}})
            except OSError:
                pass
        except Exception:
            logging.exception("http internal failure")
            self.json(500, {"error": {"code": "internal", "message": "服务内部错误，请检查日志。"}})

    do_GET = dispatch
    do_HEAD = dispatch
    do_POST = dispatch
    do_PUT = dispatch
    do_DELETE = dispatch
    do_OPTIONS = dispatch
