from __future__ import annotations

"""Startup timing state used by STARTUP_TIMING reports."""

import json
import os
import time
from pathlib import Path
from typing import Any

from .common import ensure_parent, write_json_atomic
from .paths import root_path

STARTUP_TYPE_START = "START"
STARTUP_TYPE_CREATE = "CREATE"
VALID_STARTUP_TYPES = {STARTUP_TYPE_START, STARTUP_TYPE_CREATE}
STARTUP_STATUS_SUCCESS = "SUCCESS"
STARTUP_STATUS_FAILED = "FAILED"
VALID_STARTUP_STATUSES = {STARTUP_STATUS_SUCCESS, STARTUP_STATUS_FAILED}

INTERNAL_TIMING_KEYS = {
    "startupStatus",
    "xagentUpdateCheckFinishedAtMs",
    "xagentNeedUpdate",
    "xagentDownloadStartedAtMs",
    "xagentDownloadFinishedAtMs",
    "xagentInstallStartedAtMs",
    "xagentInstallFinishedAtMs",
    "xagentStartStartedAtMs",
    "xagentReadyAtMs",
    "xagentFailedAtMs",
}


def now_ms() -> int:
    return int(time.time() * 1000)


def startup_timing_file(cfg: dict[str, Any]) -> Path:
    runtime = cfg.get("runtime", {}) or {}
    configured = str(runtime.get("startup_timing_file", "")).strip()
    if configured:
        return Path(configured)
    return Path(root_path(cfg, "startup_timing.json"))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def read_startup_timing(cfg: dict[str, Any]) -> dict[str, Any]:
    return _read_json(startup_timing_file(cfg))


def _int_ms(value: Any) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return 0


def _duration_ms(start: Any, end: Any) -> int:
    s = _int_ms(start)
    e = _int_ms(end)
    if s <= 0 or e <= 0 or e < s:
        return 0
    return e - s


def normalize_startup_type(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if text in VALID_STARTUP_TYPES else ""


def normalize_startup_status(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if text in VALID_STARTUP_STATUSES else ""


def record_update_env_startup_timing(
    cfg: dict[str, Any],
    *,
    startup_type: Any,
    sandbox_startup_ms: Any,
    sandbox_init_ms: Any,
) -> bool:
    normalized = normalize_startup_type(startup_type)
    if not normalized:
        return False

    data = read_startup_timing(cfg)
    data.update(
        {
            "reported": False,
            "startupType": normalized,
            "sandboxStartupMs": _int_ms(sandbox_startup_ms),
            "sandboxInitMs": _int_ms(sandbox_init_ms),
        }
    )
    write_json_atomic(str(startup_timing_file(cfg)), data)
    return True


def initialize_startup_timing_for_bootstrap(cfg: dict[str, Any], *, started_at_ms: int | None = None) -> None:
    data = read_startup_timing(cfg)
    initialized = {
        "reported": False,
        "startupType": normalize_startup_type(data.get("startupType")),
        "sandboxStartupMs": _int_ms(data.get("sandboxStartupMs")),
        "sandboxInitMs": _int_ms(data.get("sandboxInitMs")),
        "envPrepareStartedAtMs": int(started_at_ms or now_ms()),
    }
    for key in INTERNAL_TIMING_KEYS:
        if key == "xagentNeedUpdate":
            initialized[key] = False
        elif key == "startupStatus":
            initialized[key] = ""
        else:
            initialized[key] = 0
    write_json_atomic(str(startup_timing_file(cfg)), initialized)


def merge_xagent_startup_timing(cfg: dict[str, Any], fields: dict[str, Any]) -> None:
    if not fields:
        return
    data = read_startup_timing(cfg)
    for key, value in fields.items():
        if key not in INTERNAL_TIMING_KEYS:
            continue
        if key == "xagentNeedUpdate":
            data[key] = bool(value)
        elif key == "startupStatus":
            data[key] = normalize_startup_status(value)
        else:
            data[key] = _int_ms(value)
    write_json_atomic(str(startup_timing_file(cfg)), data)


def mark_startup_timing_reported(cfg: dict[str, Any]) -> None:
    data = read_startup_timing(cfg)
    if not data:
        return
    data["reported"] = True
    try:
        write_json_atomic(str(startup_timing_file(cfg)), data)
    except Exception:
        path = startup_timing_file(cfg)
        try:
            ensure_parent(str(path))
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            return


def build_startup_timing_metrics(data: dict[str, Any]) -> dict[str, Any] | None:
    startup_type = normalize_startup_type(data.get("startupType"))
    if not startup_type or bool(data.get("reported", False)):
        return None

    status = normalize_startup_status(data.get("startupStatus"))
    if not status or _int_ms(data.get("envPrepareStartedAtMs")) <= 0:
        return None

    if status == STARTUP_STATUS_SUCCESS:
        required = [
            "xagentUpdateCheckFinishedAtMs",
            "xagentStartStartedAtMs",
            "xagentReadyAtMs",
        ]
        if any(_int_ms(data.get(key)) <= 0 for key in required):
            return None

    need_update = bool(data.get("xagentNeedUpdate", False))
    if need_update and status == STARTUP_STATUS_SUCCESS:
        update_required = [
            "xagentDownloadStartedAtMs",
            "xagentDownloadFinishedAtMs",
            "xagentInstallStartedAtMs",
            "xagentInstallFinishedAtMs",
        ]
        if any(_int_ms(data.get(key)) <= 0 for key in update_required):
            return None

    return {
        "startupType": startup_type,
        "startupStatus": status,
        "sandboxStartupMs": _int_ms(data.get("sandboxStartupMs")),
        "sandboxInitMs": _int_ms(data.get("sandboxInitMs")),
        "sandboxEnvPrepareMs": _duration_ms(
            data.get("envPrepareStartedAtMs"),
            data.get("xagentUpdateCheckFinishedAtMs"),
        ),
        "xagentNeedUpdate": need_update,
        "xagentPkgDownloadMs": _duration_ms(
            data.get("xagentDownloadStartedAtMs"),
            data.get("xagentDownloadFinishedAtMs"),
        )
        if need_update
        else 0,
        "xagentInstallMs": _duration_ms(
            data.get("xagentInstallStartedAtMs"),
            data.get("xagentInstallFinishedAtMs"),
        )
        if need_update
        else 0,
        "xagentStartMs": _duration_ms(
            data.get("xagentStartStartedAtMs"),
            data.get("xagentReadyAtMs") if status == STARTUP_STATUS_SUCCESS else data.get("xagentFailedAtMs"),
        ),
    }
