from __future__ import annotations

"""service 下发环境变量的缓存与事件文件。"""

import json
import os
import uuid
from pathlib import Path
from typing import Any

from .common import FileLock, ensure_parent, now_iso, write_json_atomic
from .paths import root_path, tmp_path

SERVICE_ENV_ARGS = {
    "X_SANDBOX_ID": "sandbox_id",
    "X_SANDBOX_TYPE": "sandbox_type",
    "X_SANDBOX_PLATFORM": "sandbox_platform",
    "EXPERT_ENABLE_HA": "expert_enable_ha",
    "X_SANDBOX_USER_ID": "user_id",
    "X_SANDBOX_USER_NAME": "user_name",
    "ANTHROPIC_BASE_URL": "base_url",
    "ANTHROPIC_AUTH_TOKEN": "auth_token",
}


def env_cache_file(cfg: dict[str, Any]) -> str:
    runtime = cfg.get("runtime", {}) or {}
    return str(runtime.get("env_cache_file") or root_path(cfg, "env.json"))


def env_request_file(cfg: dict[str, Any]) -> str:
    runtime = cfg.get("runtime", {}) or {}
    return str(runtime.get("env_request_file") or root_path(cfg, "events", "env_requests.jsonl"))


def env_lock_file(cfg: dict[str, Any]) -> str:
    runtime = cfg.get("runtime", {}) or {}
    return str(runtime.get("env_lock_file") or root_path(cfg, "locks", "env.lock"))


def bootstrap_lock_file(cfg: dict[str, Any]) -> str:
    runtime = cfg.get("runtime", {}) or {}
    return str(runtime.get("bootstrap_lock_file") or tmp_path(cfg, "sandbox_guard_bootstrap.lock"))


def service_dynamic_items(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in ((cfg.get("env", {}) or {}).get("items", []) or []):
        if isinstance(item, dict) and str(item.get("policy", "")).strip() == "service_dynamic":
            key = str(item.get("key", "")).strip()
            if key:
                items.append(item)
    return items


def service_dynamic_keys(cfg: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for item in service_dynamic_items(cfg):
        key = str(item.get("key", "")).strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def restart_xagent_on_change_keys(cfg: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for item in service_dynamic_items(cfg):
        key = str(item.get("key", "")).strip()
        if key and _truthy(item.get("restart_xagent_on_change")) and key not in keys:
            keys.append(key)
    return keys


def read_env_cache(cfg: dict[str, Any]) -> dict[str, str]:
    p = Path(env_cache_file(cfg))
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    values: dict[str, str] = {}
    allowed = set(service_dynamic_keys(cfg))
    for key, value in data.items():
        if not isinstance(key, str) or key not in allowed:
            continue
        if value is None:
            continue
        text = str(value)
        if text != "":
            values[key] = text
    return values


def write_env_cache(cfg: dict[str, Any], values: dict[str, str]) -> None:
    allowed = set(service_dynamic_keys(cfg))
    payload = {key: str(value) for key, value in values.items() if key in allowed and str(value) != ""}
    write_json_atomic(env_cache_file(cfg), payload)


def restore_env_cache_to_process(cfg: dict[str, Any]) -> list[str]:
    restored: list[str] = []
    for key, value in read_env_cache(cfg).items():
        if value == "":
            continue
        os.environ[key] = value
        restored.append(key)
    return restored


def build_service_env_from_args(cfg: dict[str, Any], args: dict[str, str | None], old_cache: dict[str, str]) -> dict[str, str]:
    values = dict(old_cache)
    for key in service_dynamic_keys(cfg):
        arg_name = SERVICE_ENV_ARGS.get(key)
        if not arg_name:
            continue
        raw = args.get(arg_name)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            values[key] = text
    return values


def changed_service_dynamic_keys(cfg: dict[str, Any], old_cache: dict[str, str], new_cache: dict[str, str]) -> list[str]:
    changed: list[str] = []
    for key in restart_xagent_on_change_keys(cfg):
        if old_cache.get(key) != new_cache.get(key):
            changed.append(key)
    return changed


def append_env_request_unlocked(cfg: dict[str, Any], request: dict[str, Any]) -> None:
    path = env_request_file(cfg)
    ensure_parent(path)
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n")


def append_xagent_env_changed_unlocked(cfg: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    request = {
        "type": "xagent_env_changed",
        "keys": list(keys),
        "requested_at": now_iso(),
        "request_id": str(uuid.uuid4()),
    }
    append_env_request_unlocked(cfg, request)
    return request


def load_env_requests(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    path = Path(env_request_file(cfg))
    if not path.exists():
        return []
    requests: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if isinstance(payload, dict):
            requests.append(payload)
    return requests


def remove_consumed_env_requests(cfg: dict[str, Any], request_ids: set[str]) -> None:
    if not request_ids:
        return
    lock_path = env_lock_file(cfg)
    with FileLock(lock_path):
        current = load_env_requests(cfg)
        remaining = []
        for req in current:
            rid = str(req.get("request_id", "")).strip()
            if rid and rid in request_ids:
                continue
            remaining.append(req)
        path = env_request_file(cfg)
        ensure_parent(path)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            for req in remaining:
                fp.write(json.dumps(req, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
