from __future__ import annotations

"""Bound PM2 daemon log growth without invoking PM2 commands."""

import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import log, now_iso


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def reconcile_pm2_log_cleanup(
    cfg: dict[str, Any],
    state: dict[str, Any],
    prev_state: dict[str, Any],
) -> None:
    cleanup_cfg = (cfg.get("daemon", {}) or {}).get("pm2_log_cleanup", {}) or {}
    enabled = bool(cleanup_cfg.get("enabled", True))
    previous = prev_state.get("pm2_log_cleanup", {}) or {}
    pm2_home = Path(str((cfg.get("runtime", {}) or {}).get("pm2_home", "/home/x/.pm2"))).expanduser()
    log_path = pm2_home / "pm2.log"
    max_size_bytes = max(1, int(cleanup_cfg.get("max_size_mb", 20))) * 1024 * 1024
    payload: dict[str, Any] = {
        "enabled": enabled,
        "status": "skipped",
        "last_run_at": previous.get("last_run_at"),
        "path": str(log_path),
        "max_size_bytes": max_size_bytes,
        "size_before_bytes": 0,
        "truncated": False,
        "message": "",
    }
    state["pm2_log_cleanup"] = payload
    if not enabled:
        payload["message"] = "disabled"
        return

    interval_seconds = max(1, int(cleanup_cfg.get("interval_seconds", 3600)))
    last_run_at = _parse_iso(str(payload.get("last_run_at") or ""))
    now = datetime.now(timezone.utc)
    if last_run_at is not None and (now - last_run_at).total_seconds() < interval_seconds:
        payload["message"] = "interval not due"
        return

    payload["last_run_at"] = now_iso()
    try:
        file_stat = log_path.lstat()
    except FileNotFoundError:
        payload["status"] = "ok"
        payload["message"] = "log file missing"
        return
    except Exception as exc:
        payload["status"] = "degraded"
        payload["message"] = str(exc)
        log("warn", "daemon.pm2_log.stat_failed", "读取 PM2 daemon 日志大小失败", path=str(log_path), error=str(exc))
        return

    if not stat.S_ISREG(file_stat.st_mode):
        payload["status"] = "degraded"
        payload["message"] = "log path is not a regular file"
        log("warn", "daemon.pm2_log.invalid_file", "PM2 daemon 日志路径不是普通文件，跳过清理", path=str(log_path))
        return

    payload["size_before_bytes"] = file_stat.st_size
    if file_stat.st_size <= max_size_bytes:
        payload["status"] = "ok"
        payload["message"] = "within limit"
        return

    try:
        os.truncate(log_path, 0)
    except Exception as exc:
        payload["status"] = "degraded"
        payload["message"] = str(exc)
        log(
            "warn",
            "daemon.pm2_log.truncate_failed",
            "截断 PM2 daemon 日志失败",
            path=str(log_path),
            size_bytes=file_stat.st_size,
            max_size_bytes=max_size_bytes,
            error=str(exc),
        )
        return

    payload["status"] = "ok"
    payload["truncated"] = True
    payload["message"] = "truncated"
    log(
        "info",
        "daemon.pm2_log.truncated",
        "PM2 daemon 日志超过限制，已原地截断",
        path=str(log_path),
        size_bytes=file_stat.st_size,
        max_size_bytes=max_size_bytes,
    )
