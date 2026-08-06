from __future__ import annotations

"""Keep the external heartbeat aligned with confirmed xagent availability."""

from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .common import log, now_iso
from .http_client import HttpJsonError, http_get_json

HeartbeatDecision = Literal["enable", "disable", "keep"]


def _control_config(cfg: dict[str, Any]) -> dict[str, Any]:
    value = cfg.get("xagent_heartbeat_control", {})
    return value if isinstance(value, dict) else {}


def _process_config(cfg: dict[str, Any], name: str) -> dict[str, Any] | None:
    for process in cfg.get("processes", []) or []:
        if isinstance(process, dict) and str(process.get("name", "")).strip() == name:
            return process
    return None


def _heartbeat_url(cfg: dict[str, Any], heartbeat_process: str) -> str:
    process = _process_config(cfg, heartbeat_process)
    if process is None:
        return ""
    health = process.get("health_check", {})
    if not isinstance(health, dict):
        return ""
    return str(health.get("url", "")).strip()


def _action_url(url: str, enabled: bool) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["action"] = "true" if enabled else "false"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _timeout_seconds(cfg: dict[str, Any]) -> float:
    try:
        return min(3.0, max(0.1, float(_control_config(cfg).get("timeout_seconds", 3))))
    except Exception:
        return 3.0


def _health_failure_threshold(proc: dict[str, Any] | None) -> int:
    if proc is None:
        return 0
    try:
        return max(0, int(proc.get("health_failure_threshold", 0)))
    except Exception:
        return 0


def decide_xagent_heartbeat(
    xagent_state: dict[str, Any],
    xagent_config: dict[str, Any] | None,
) -> HeartbeatDecision:
    """Return only confirmed transitions; uncertain and planned states are kept."""
    upgrade_state = str(xagent_state.get("upgrade_state", "stable")).strip()
    if upgrade_state in {"pending", "upgrading"}:
        return "keep"

    if str(xagent_state.get("status", "")).strip() == "healthy":
        return "enable"

    raw_status = str(xagent_state.get("manager_raw_status", "")).strip().lower()
    if raw_status == "errored":
        return "disable"

    threshold = _health_failure_threshold(xagent_config)
    try:
        failed_count = int(xagent_state.get("health_failed_count", 0) or 0)
    except Exception:
        failed_count = 0
    if threshold > 0 and failed_count >= threshold:
        return "disable"

    last_action = str(xagent_state.get("last_action", "")).strip()
    last_result = str(xagent_state.get("last_action_result", "")).strip()
    if last_result == "failed" and last_action in {"start", "restart", "pre_recover"}:
        return "disable"

    message = str(xagent_state.get("message", "")).strip()
    if message.startswith("health failure threshold reached"):
        return "disable"

    return "keep"


def _previous_record(prev: dict[str, Any]) -> dict[str, Any]:
    previous = prev.get("xagent_heartbeat_control", {})
    if not isinstance(previous, dict):
        previous = {}
    return {
        "enabled": bool(previous.get("enabled", False)),
        "decision": str(previous.get("decision", "keep")),
        "desired_running": previous.get("desired_running"),
        "actual_running": previous.get("actual_running"),
        "last_observed_at": previous.get("last_observed_at"),
        "last_action": previous.get("last_action"),
        "last_action_result": previous.get("last_action_result"),
        "last_action_at": previous.get("last_action_at"),
        "last_error": str(previous.get("last_error", "")),
    }


def _set_error(rec: dict[str, Any], previous_error: str, error: str, event: str, message: str) -> None:
    rec["last_error"] = error
    if error != previous_error:
        log("warn", event, message, error=error)


def disable_heartbeat_before_process_stop(cfg: dict[str, Any], process_name: str) -> bool | None:
    """Best-effort Nacos deregistration before the managed process is stopped."""
    control = _control_config(cfg)
    if not bool(control.get("enabled", False)):
        return None
    heartbeat_process = str(control.get("heartbeat_process", "nacos-heartbeat")).strip() or "nacos-heartbeat"
    if str(process_name).strip() != heartbeat_process:
        return None
    url = _heartbeat_url(cfg, heartbeat_process)
    if not url:
        log(
            "warn",
            "daemon.xagent_heartbeat.pre_stop_config_invalid",
            "停止 Nacos 心跳程序前无法执行注销，health_check.url 为空",
            process=heartbeat_process,
        )
        return False
    try:
        response = http_get_json(
            _action_url(url, False),
            timeout_seconds=_timeout_seconds(cfg),
        )
    except HttpJsonError as exc:
        log(
            "warn",
            "daemon.xagent_heartbeat.pre_stop_failed",
            "停止 Nacos 心跳程序前注销失败，继续停止进程",
            process=heartbeat_process,
            error=str(exc),
        )
        return False
    log(
        "info",
        "daemon.xagent_heartbeat.pre_stop_applied",
        "停止 Nacos 心跳程序前已请求注销",
        process=heartbeat_process,
        response=response,
    )
    return True


def reconcile_xagent_heartbeat_control(
    cfg: dict[str, Any],
    state: dict[str, Any],
    prev: dict[str, Any],
) -> None:
    """Reconcile the Nacos heartbeat program without affecting daemon health."""
    control = _control_config(cfg)
    enabled = bool(control.get("enabled", False))
    rec = _previous_record(prev)
    previous_error = rec["last_error"]
    rec["enabled"] = enabled
    state["xagent_heartbeat_control"] = rec
    if not enabled:
        rec["decision"] = "keep"
        rec["desired_running"] = None
        rec["last_error"] = ""
        return

    xagent_process = str(control.get("xagent_process", "xagent")).strip() or "xagent"
    heartbeat_process = str(control.get("heartbeat_process", "nacos-heartbeat")).strip() or "nacos-heartbeat"
    process_states = state.get("processes", {})
    if not isinstance(process_states, dict):
        process_states = {}
    heartbeat_state = process_states.get(heartbeat_process, {})
    if not isinstance(heartbeat_state, dict) or not bool(heartbeat_state.get("applicable", False)):
        rec["decision"] = "disable"
        rec["desired_running"] = False
        rec["actual_running"] = False
        rec["last_error"] = ""
        return

    url = _heartbeat_url(cfg, heartbeat_process)
    if not url:
        _set_error(
            rec,
            previous_error,
            f"process {heartbeat_process} health_check.url is empty",
            "daemon.xagent_heartbeat.config_invalid",
            "xagent 心跳控制配置不完整",
        )
        return

    xagent_state = process_states.get(xagent_process, {})
    if not isinstance(xagent_state, dict):
        xagent_state = {}
    decision = decide_xagent_heartbeat(
        xagent_state,
        _process_config(cfg, xagent_process),
    )
    rec["decision"] = decision
    if decision == "enable":
        rec["desired_running"] = True
    elif decision == "disable":
        rec["desired_running"] = False

    try:
        payload = http_get_json(url, timeout_seconds=_timeout_seconds(cfg))
    except HttpJsonError as exc:
        _set_error(
            rec,
            previous_error,
            str(exc),
            "daemon.xagent_heartbeat.probe_failed",
            "Nacos 心跳程序状态查询失败",
        )
        return

    actual_running = payload.get("running")
    if not isinstance(actual_running, bool):
        _set_error(
            rec,
            previous_error,
            "Nacos heartbeat health response missing boolean running",
            "daemon.xagent_heartbeat.probe_invalid",
            "Nacos 心跳程序状态响应无效",
        )
        return
    rec["actual_running"] = actual_running
    rec["last_observed_at"] = now_iso()
    rec["last_error"] = ""

    # The first uncertain cycle adopts the program's actual state. Later keep
    # decisions can then restore that state if the heartbeat program restarts.
    if decision == "keep" and not isinstance(rec.get("desired_running"), bool):
        rec["desired_running"] = actual_running

    desired_running = rec.get("desired_running")
    if not isinstance(desired_running, bool) or desired_running == actual_running:
        return

    action = "start" if desired_running else "stop"
    try:
        response = http_get_json(
            _action_url(url, desired_running),
            timeout_seconds=_timeout_seconds(cfg),
        )
    except HttpJsonError as exc:
        rec["last_action"] = action
        rec["last_action_result"] = "failed"
        rec["last_action_at"] = now_iso()
        _set_error(
            rec,
            previous_error,
            str(exc),
            "daemon.xagent_heartbeat.action_failed",
            "Nacos 心跳程序状态调整失败",
        )
        return

    response_running = response.get("running")
    rec["actual_running"] = response_running if isinstance(response_running, bool) else desired_running
    rec["last_action"] = action
    rec["last_action_result"] = "success"
    rec["last_action_at"] = now_iso()
    rec["last_error"] = ""
    log(
        "info",
        "daemon.xagent_heartbeat.action_applied",
        "已根据 xagent 状态调整 Nacos 心跳",
        action=action,
        xagent_status=str(xagent_state.get("status", "")),
        xagent_raw_status=str(xagent_state.get("manager_raw_status", "")),
    )
