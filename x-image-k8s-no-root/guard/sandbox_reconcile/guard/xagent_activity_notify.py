from __future__ import annotations

"""Periodic xagent activity probe and sandbox-service notification."""

import time
from typing import Any
from urllib.parse import quote

from .common import log
from .http_client import HttpJsonError, http_post_empty
from .report import resolve_sandbox_id
from .xagent_sessions import probe_configured_xagent_sessions

ACTIVITY_PATH_PREFIX = "/v1/runners/sandbox"
ACTIVITY_PATH_SUFFIX = "/update-activity"
DEFAULT_INTERVAL_SECONDS = 3600
MAX_NOTIFY_TIMEOUT_SECONDS = 3.0


def now_ms() -> int:
    return int(time.time() * 1000)


def _activity_config(cfg: dict[str, Any]) -> dict[str, Any]:
    value = cfg.get("xagent_activity_notify", {})
    return value if isinstance(value, dict) else {}


def _interval_ms(cfg: dict[str, Any]) -> int:
    raw = _activity_config(cfg).get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    try:
        seconds = int(raw)
    except Exception:
        seconds = DEFAULT_INTERVAL_SECONDS
    return max(1, seconds) * 1000


def _notify_timeout_seconds(cfg: dict[str, Any]) -> float:
    raw = _activity_config(cfg).get("timeout_seconds", MAX_NOTIFY_TIMEOUT_SECONDS)
    try:
        timeout = float(raw)
    except Exception:
        timeout = MAX_NOTIFY_TIMEOUT_SECONDS
    return min(MAX_NOTIFY_TIMEOUT_SECONDS, max(0.1, timeout))


def _initial_state(enabled: bool, next_probe_at_ms: int = 0) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "next_probe_at_ms": next_probe_at_ms,
        "last_probe_at_ms": 0,
        "last_session_count": None,
        "last_notify_at_ms": 0,
        "last_error": "",
    }


def initialize_xagent_activity_notify(
    cfg: dict[str, Any],
    state: dict[str, Any],
    now_ms_value: int | None = None,
) -> None:
    """Initialize the next probe at sandbox bootstrap time."""
    enabled = bool(_activity_config(cfg).get("enabled", False))
    current = now_ms() if now_ms_value is None else int(now_ms_value)
    state["xagent_activity_notify"] = _initial_state(
        enabled,
        current + _interval_ms(cfg) if enabled else 0,
    )
    if enabled:
        log(
            "info",
            "daemon.xagent_activity.initialized",
            "xagent 会话活动探测已初始化",
            next_probe_at_ms=state["xagent_activity_notify"]["next_probe_at_ms"],
        )


def _copy_previous_state(prev: dict[str, Any]) -> dict[str, Any]:
    previous = prev.get("xagent_activity_notify", {})
    if not isinstance(previous, dict):
        previous = {}
    result = _initial_state(bool(previous.get("enabled", False)))
    for key in result:
        if key in previous:
            result[key] = previous[key]
    return result


def reconcile_xagent_activity_notify(
    cfg: dict[str, Any],
    state: dict[str, Any],
    prev: dict[str, Any],
    now_ms_value: int | None = None,
) -> None:
    """Run one due activity probe without affecting daemon health."""
    notify_cfg = _activity_config(cfg)
    enabled = bool(notify_cfg.get("enabled", False))
    current = now_ms() if now_ms_value is None else int(now_ms_value)
    rec = _copy_previous_state(prev)
    rec["enabled"] = enabled
    state["xagent_activity_notify"] = rec

    if not enabled:
        rec["next_probe_at_ms"] = 0
        return

    try:
        next_probe_at_ms = int(rec.get("next_probe_at_ms", 0) or 0)
    except Exception:
        next_probe_at_ms = 0
    previous_activity = prev.get("xagent_activity_notify", {})
    previous_enabled = (
        bool(previous_activity.get("enabled", False))
        if isinstance(previous_activity, dict)
        else False
    )
    if not previous_enabled or next_probe_at_ms <= 0:
        rec["next_probe_at_ms"] = current + _interval_ms(cfg)
        rec["last_error"] = ""
        log(
            "info",
            "daemon.xagent_activity.initialized",
            "xagent 会话活动探测已初始化",
            next_probe_at_ms=rec["next_probe_at_ms"],
        )
        return
    if current < next_probe_at_ms:
        return

    # Advance first: every due period gets one attempt, with no immediate retry.
    rec["next_probe_at_ms"] = current + _interval_ms(cfg)
    rec["last_probe_at_ms"] = current
    ok, total, error = probe_configured_xagent_sessions(cfg)
    if not ok or total is None:
        rec["last_error"] = error or "xagent session probe failed"
        log(
            "warn",
            "daemon.xagent_activity.probe_failed",
            "xagent 会话活动探测失败",
            error=rec["last_error"],
            next_probe_at_ms=rec["next_probe_at_ms"],
        )
        return

    rec["last_session_count"] = total
    rec["last_error"] = ""
    if total <= 0:
        log(
            "info",
            "daemon.xagent_activity.no_sessions",
            "xagent 当前无活动会话，无需通知 sandbox service",
            session_count=total,
            next_probe_at_ms=rec["next_probe_at_ms"],
        )
        return

    sandbox_id = resolve_sandbox_id(cfg)
    if not sandbox_id:
        rec["last_error"] = "sandbox id is empty"
        log(
            "warn",
            "daemon.xagent_activity.sandbox_id_missing",
            "xagent 存在活动会话，但无法解析 sandboxId",
            session_count=total,
        )
        return

    base_url = str(notify_cfg.get("base_url", "")).strip().rstrip("/")
    target = f"{base_url}{ACTIVITY_PATH_PREFIX}/{quote(sandbox_id, safe='')}{ACTIVITY_PATH_SUFFIX}"
    rec["last_notify_at_ms"] = current
    try:
        status, response_body = http_post_empty(
            target,
            timeout_seconds=_notify_timeout_seconds(cfg),
            max_response_bytes=4096,
        )
        if status < 200 or status >= 300:
            rec["last_error"] = f"http status {status}"
        log(
            "info" if 200 <= status < 300 else "warn",
            "daemon.xagent_activity.notified",
            "xagent 活动会话已通知 sandbox service",
            sandbox_id=sandbox_id,
            session_count=total,
            http_status=status,
            response_body=response_body,
            next_probe_at_ms=rec["next_probe_at_ms"],
        )
    except HttpJsonError as exc:
        rec["last_error"] = str(exc)
        log(
            "warn",
            "daemon.xagent_activity.notify_failed",
            "xagent 活动会话通知 sandbox service 失败",
            sandbox_id=sandbox_id,
            session_count=total,
            error=str(exc),
            next_probe_at_ms=rec["next_probe_at_ms"],
        )
