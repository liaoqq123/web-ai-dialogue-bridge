#!/usr/bin/env python3
"""Run the web bridge or call its local worker API. Python 3.11+, no packages."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser

from dialogue_bridge.adapters import DeliveryError, make_adapter
from dialogue_bridge.config import DEFAULTS, credentials, load_config, read_json, validate_config, write_json
from dialogue_bridge.errors import BridgeError
from dialogue_bridge.runtime import service_lock
from dialogue_bridge.server import Application, BridgeHTTPServer, encoded


ROOT = Path(__file__).resolve().parent


def output(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2))


def call_local(data_dir: Path, path: str, body: dict | None = None, *, role="worker", wait_for_delivery=False):
    runtime = read_json(data_dir / "runtime.json")
    tokens = read_json(data_dir / "credentials.json")
    port = runtime.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise BridgeError("runtime", "服务运行信息无效。")
    host = runtime.get("host", "127.0.0.1")
    host = "127.0.0.1" if host == "0.0.0.0" else host
    try:
        if ipaddress.ip_address(host).version != 4:
            raise ValueError()
    except ValueError as exc:
        raise BridgeError("runtime", "服务运行主机无效。") from exc
    request = Request("http://" + host + ":" + str(port) + path,
                      data=encoded(body) if body is not None else None,
                      headers={"Authorization": "Bearer " + tokens[role + "_token"],
                               "Content-Type": "application/json"},
                      method="POST" if body is not None else "GET")
    deadline = time.monotonic() + (45 if wait_for_delivery else 0)
    while True:
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=20) as response:
                return json.loads(response.read(4 * 1024 * 1024).decode("utf-8"))
        except HTTPError as exc:
            try:
                problem = json.loads(exc.read(65536).decode("utf-8")).get("error", {})
            except (ValueError, UnicodeError):
                problem = {}
            finally:
                exc.close()
            if problem.get("code") == "reply_not_delivered" and time.monotonic() < deadline:
                time.sleep(0.3)
                continue
            raise BridgeError(problem.get("code", "http_error"), problem.get("message", "服务拒绝该请求。"), exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BridgeError("offline", "无法连接本地桥接服务。请先启动服务。") from exc


def current_url(data_dir: Path, host: str | None = None) -> str:
    runtime = read_json(data_dir / "runtime.json")
    tokens = read_json(data_dir / "credentials.json")
    host = host or runtime.get("host", "127.0.0.1")
    host = "127.0.0.1" if host == "0.0.0.0" else host
    if not host or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in host):
        raise BridgeError("input", "网址主机须为 IPv4 地址或主机名。")
    return "http://" + host + ":" + str(runtime["port"]) + "/#token=" + quote(tokens["browser_token"], safe="")


def configure_logging(data_dir: Path):
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[RotatingFileHandler(logs / "server.log", maxBytes=2 * 1024 * 1024,
                                                      backupCount=3, encoding="utf-8")], force=True)


def effective_config(args, data_dir):
    config = load_config(data_dir, Path(args.config).resolve() if getattr(args, "config", None) else None)
    for key in ("host", "port", "adapter"):
        value = getattr(args, key, None)
        if value is not None:
            config[key] = value
    return validate_config(config)


def serve(args, data_dir):
    with service_lock(data_dir):
        configure_logging(data_dir)
        config = effective_config(args, data_dir)
        app = Application(data_dir, config, cli_path=Path(__file__).resolve())
        server = BridgeHTTPServer((config["host"], config["port"]), app)
        write_json(data_dir / "runtime.json", {
            "pid": os.getpid(), "port": server.server_port, "host": config["host"],
            "adapter": config["adapter"], "started_at": datetime.now(timezone.utc).isoformat(),
        })
        url = current_url(data_dir)
        (data_dir / "open.url").write_text(url + "\n", encoding="utf-8")
        if not getattr(args, "quiet", False):
            print("网页地址（含访问凭据，请只分享给需要使用的人）：\n" + url, flush=True)
        app.dispatcher.start()
        if args.open:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            app.dispatcher.close()
            server.server_close()
            logging.info("service stopped")


def start(args, data_dir):
    if (data_dir / "runtime.json").exists():
        try:
            call_local(data_dir, "/api/config", role="browser")
            url = current_url(data_dir)
            print(url)
            if args.open:
                webbrowser.open(url)
            return
        except BridgeError:
            pass
    data_dir.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(ROOT / "bridge.py"), "--data-dir", str(data_dir), "serve", "--quiet"]
    for key in ("host", "port", "adapter", "config"):
        value = getattr(args, key, None)
        if value is not None:
            command += ["--" + key, str(Path(value).resolve()) if key == "config" else str(value)]
    log = (data_dir / "startup.log").open("ab")
    options = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT, "cwd": ROOT}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **options)
    finally:
        log.close()
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BridgeError("startup", "启动失败。请查看数据目录 startup.log。")
        try:
            call_local(data_dir, "/api/config", role="browser")
            print(current_url(data_dir))
            if args.open:
                webbrowser.open(current_url(data_dir))
            return
        except BridgeError:
            time.sleep(0.15)
    raise BridgeError("startup", "启动尚未就绪。请查看数据目录日志；不会自动重复启动。")


def bind_desktop(args, data_dir):
    # A local operator chooses the project. The browser has no binding endpoint.
    with service_lock(data_dir):
        config = load_config(data_dir)
        if args.binding_file:
            binding = read_json(Path(args.binding_file).resolve())
        else:
            binding = {"schema_version": 1, "project_id": args.project_id,
                       "project_path": str(Path(args.project_dir).resolve()), "host_id": "local",
                       "caller_thread_id": args.caller_thread_id or os.environ.get("CODEX_THREAD_ID"),
                       "pipe_path": args.pipe or os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")}
        if not isinstance(binding.get("project_path"), str) or not Path(binding["project_path"]).is_dir():
            raise BridgeError("binding", "请指定真实存在的 Codex 本地项目目录。")
        binding_path = data_dir / "desktop-binding.json"
        candidate = data_dir / ("desktop-check-" + secrets.token_hex(8) + ".json")
        write_json(candidate, binding)
        candidate_config = {**config, "adapter": "codex-desktop", "desktop_binding_file": str(candidate),
                            "project_dir": str(Path(binding["project_path"]).resolve())}
        try:
            adapter = make_adapter(candidate_config, data_dir=data_dir,
                                   project_dir=Path(candidate_config["project_dir"]), cli_path=ROOT / "bridge.py")
            checked = adapter.check()
            write_json(binding_path, binding)
            candidate_config["desktop_binding_file"] = str(binding_path)
            write_json(data_dir / "config.json", candidate_config)
            credentials(data_dir)
            output(checked)
        finally:
            candidate.unlink(missing_ok=True)


def request_key(args, prefix, payload):
    import hashlib
    return args.request_key or prefix + "-" + hashlib.sha256(encoded(payload)).hexdigest()


def question_request_key(args, data_dir, body):
    if args.request_key:
        return args.request_key
    # A repeated question in a later round must get a new revision. A lost
    # response during this round must retain its original idempotency key.
    from dialogue_bridge.store import _questions
    normalized = _questions(body["questions"])
    task = call_local(data_dir, "/api/worker/tasks/" + args.task_id)["task"]
    group = task.get("question_group")
    same_pending = group and group["status"] == "pending" and group["questions"] == normalized
    revision = group["revision"] if same_pending else task["revision"] + 1
    return request_key(args, "questions-" + args.task_id + "-r" + str(revision), {"questions": normalized})


def parser():
    result = argparse.ArgumentParser(description="数据上传、网页回复与原 AI 对话回调桥。")
    result.add_argument("--data-dir", default=str(ROOT / ".bridge"), help="本地数据目录，默认项目 .bridge")
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="初始化演示配置和随机凭据")
    init.add_argument("--adapter", choices=["demo", "webhook"], default="demo")
    for name in ("serve", "start"):
        command = commands.add_parser(name, help="前台运行" if name == "serve" else "后台运行")
        command.add_argument("--host")
        command.add_argument("--port", type=int)
        command.add_argument("--adapter", choices=["demo", "webhook", "codex-desktop"])
        command.add_argument("--config")
        command.add_argument("--open", action="store_true", help="打开默认浏览器")
        command.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    commands.add_parser("stop", help="只关闭当前数据目录对应的服务")
    url = commands.add_parser("url", help="显示带浏览器凭据的网址")
    url.add_argument("--host", help="内网访问时替换网址中的主机名或 IPv4")
    check = commands.add_parser("check", help="只读检查配置或桌面项目绑定")
    check.add_argument("--config")
    binding = commands.add_parser("bind-desktop", help="绑定本地桌面项目，并进行只读核对")
    binding.add_argument("--binding-file", help="已存在的本地绑定 JSON；不会上传")
    binding.add_argument("--project-id")
    binding.add_argument("--project-dir")
    binding.add_argument("--caller-thread-id")
    binding.add_argument("--pipe")
    commands.add_parser("tasks", help="读取任务列表（worker）")
    task = commands.add_parser("task", help="读取任务和文件本机路径（worker）")
    task.add_argument("--task-id", required=True)
    for name in ("publish-questions", "complete"):
        command = commands.add_parser(name, help="发布网页问题" if name == "publish-questions" else "提交完成结果")
        command.add_argument("--task-id", required=True)
        command.add_argument("--file", required=True, help="UTF-8 JSON 文件")
        command.add_argument("--request-key", help="省略时按任务、操作和内容生成稳定键")
    resolve = commands.add_parser("resolve-event", help="管理员核对原动作后补录 unknown 回执，不重发")
    resolve.add_argument("--task-id", required=True)
    resolve.add_argument("--event-id", required=True)
    resolve.add_argument("--file", required=True, help="已核实的原对话 receipt JSON")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    data_dir = Path(args.data_dir).expanduser().resolve()
    try:
        if args.command == "init":
            with service_lock(data_dir):
                if (data_dir / "config.json").exists():
                    raise BridgeError("exists", "已有配置，初始化不会覆盖。")
                write_json(data_dir / "config.json", {**DEFAULTS, "adapter": args.adapter})
                credentials(data_dir)
            output({"initialized": True, "adapter": args.adapter})
        elif args.command == "serve":
            serve(args, data_dir)
        elif args.command == "start":
            start(args, data_dir)
        elif args.command == "stop":
            output(call_local(data_dir, "/api/worker/shutdown", {}))
        elif args.command == "url":
            print(current_url(data_dir, args.host))
        elif args.command == "bind-desktop":
            if not args.binding_file and not (args.project_id and args.project_dir):
                raise BridgeError("binding", "请提供 --binding-file，或同时提供 --project-id 和 --project-dir。")
            bind_desktop(args, data_dir)
        elif args.command == "check":
            config = load_config(data_dir, Path(args.config).resolve() if args.config else None)
            adapter = make_adapter(config, data_dir=data_dir, project_dir=Path(config.get("project_dir", ROOT)),
                                   cli_path=ROOT / "bridge.py")
            output(adapter.check())
        elif args.command in {"tasks", "task"}:
            path = "/api/worker/tasks" + ("/" + args.task_id if args.command == "task" else "")
            output(call_local(data_dir, path))
        elif args.command in {"publish-questions", "complete"}:
            content = read_json(Path(args.file).resolve())
            if args.command == "publish-questions":
                if set(content) != {"questions"}:
                    raise BridgeError("input", "问题文件必须为 {\"questions\": [...]}。")
                body = {"questions": content["questions"]}
                endpoint = "questions"
            else:
                body, endpoint = {"result": content}, "complete"
            body["request_key"] = (question_request_key(args, data_dir, body) if args.command == "publish-questions"
                                   else request_key(args, args.command + "-" + args.task_id, body))
            output(call_local(data_dir, "/api/worker/tasks/" + args.task_id + "/" + endpoint, body,
                              wait_for_delivery=args.command == "publish-questions"))
        elif args.command == "resolve-event":
            receipt = read_json(Path(args.file).resolve())
            output(call_local(data_dir, "/api/worker/tasks/" + args.task_id + "/events/" + args.event_id + "/resolve",
                              {"receipt": receipt}))
        return 0
    except (BridgeError, DeliveryError) as exc:
        output({"error": {"code": getattr(exc, "code", "adapter"), "message": str(exc)}})
        return 1
    except OSError:
        output({"error": {"code": "local_io", "message": "本地读写或监听失败，请检查目录权限和端口。"}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
