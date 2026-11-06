"""Local-only configuration and credentials; never sent to the browser."""
from __future__ import annotations

import json
import ipaddress
import os
from pathlib import Path
import secrets

from .errors import BridgeError


DEFAULTS = {
    "adapter": "demo",
    "host": "127.0.0.1",
    "port": 8765,
    "max_file_bytes": 4 * 1024 * 1024,
    "max_total_bytes": 12 * 1024 * 1024,
    "max_files": 10,
    "callback_timeout": 30,
    "allowed_hosts": [],
}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise BridgeError("local_config", "本地 JSON 配置无法读取。") from exc
    if not isinstance(result, dict):
        raise BridgeError("local_config", "本地 JSON 必须是对象。")
    return result


def load_config(data_dir: Path, config_file: Path | None = None) -> dict:
    path = config_file or data_dir / "config.json"
    saved = read_json(path) if path.exists() else {}
    config = {**DEFAULTS, **saved}
    return validate_config(config)


def validate_config(config: dict) -> dict:
    if config["adapter"] not in {"demo", "webhook", "codex-desktop"}:
        raise BridgeError("local_config", "adapter 必须为 demo、webhook 或 codex-desktop。")
    try:
        if not isinstance(config["host"], str) or ipaddress.ip_address(config["host"]).version != 4:
            raise ValueError()
    except ValueError as exc:
        raise BridgeError("local_config", "host 必须是本机 IPv4 监听地址。") from exc
    if type(config["port"]) is not int or not 0 <= config["port"] <= 65535:
        raise BridgeError("local_config", "port 必须为 0 至 65535 的整数。")
    for key in ("max_file_bytes", "max_total_bytes", "max_files"):
        if type(config[key]) is not int or not 1 <= config[key] <= 64 * 1024 * 1024:
            raise BridgeError("local_config", key + " 必须为范围内的正整数。")
    if config["max_file_bytes"] > config["max_total_bytes"] or config["max_files"] > 20 or config["max_file_bytes"] > 20 * 1024 * 1024:
        raise BridgeError("local_config", "上传数量或总量限制无效。")
    if not isinstance(config["allowed_hosts"], list) or not all(
        isinstance(host, str) and host and "/" not in host and ":" not in host and not any(ord(c) < 33 for c in host)
        for host in config["allowed_hosts"]
    ):
        raise BridgeError("local_config", "allowed_hosts 只接受主机名或 IPv4 地址列表。")
    return config


def credentials(data_dir: Path) -> dict:
    path = data_dir / "credentials.json"
    if path.exists():
        value = read_json(path)
    else:
        value = {"browser_token": secrets.token_urlsafe(32), "worker_token": secrets.token_urlsafe(32)}
        # The service holds a process lock before initializing this file.
        write_json(path, value)
    if set(value) != {"browser_token", "worker_token"} or not all(
        isinstance(token, str) and 32 <= len(token) <= 256 for token in value.values()
    ) or value["browser_token"] == value["worker_token"]:
        raise BridgeError("local_config", "本地凭据格式无效。")
    return value
