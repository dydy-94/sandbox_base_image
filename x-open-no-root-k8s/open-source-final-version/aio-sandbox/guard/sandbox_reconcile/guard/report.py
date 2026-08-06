from __future__ import annotations

"""Sandbox runtime report support."""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .common import FileLock, ensure_parent, log
from .constants import APP_VERSION
from .env_store import read_env_cache
from .http_client import http_post_json_no_response
from .paths import root_path
from .process_applicability import process_is_applicable
from .resources import current_disk_used_percent
from .startup_timing import build_startup_timing_metrics, mark_startup_timing_reported, read_startup_timing

REPORT_SANDBOX_STATUS = "SANDBOX_STATUS"
REPORT_UPGRADE_FAILED = "UPGRADE_FAILED"
REPORT_STARTUP_TIMING = "STARTUP_TIMING"
DAEMON_STARTED_AT_MS = int(time.time() * 1000)

KNOWN_REPORT_TYPES = {
    REPORT_SANDBOX_STATUS,
    REPORT_UPGRADE_FAILED,
    REPORT_STARTUP_TIMING,
}


def now_ms() -> int:
    return int(time.time() * 1000)


def report_request_file(cfg: dict[str, Any]) -> str:
    report_cfg = cfg.get("report", {}) or {}
    return str(report_cfg.get("request_file") or root_path(cfg, "events", "report_requests.jsonl"))


def report_lock_file(cfg: dict[str, Any]) -> str:
    report_cfg = cfg.get("report", {}) or {}
    return str(report_cfg.get("lock_file") or root_path(cfg, "locks", "report.lock"))


def _normalize_report_type(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if text in KNOWN_REPORT_TYPES else ""


def _legacy_base_url(endpoint: str) -> str:
    text = str(endpoint or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except Exception:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def report_base_url(cfg: dict[str, Any]) -> str:
    report_cfg = cfg.get("report", {}) or {}
    base_url = str(report_cfg.get("base_url", "") or "").strip().rstrip("/")
    if base_url:
        return base_url
    return _legacy_base_url(str(report_cfg.get("endpoint", "") or ""))


def report_type_config(cfg: dict[str, Any], report_type: str) -> dict[str, Any]:
    report_cfg = cfg.get("report", {}) or {}
    types = report_cfg.get("types", {})
    if isinstance(types, dict):
        item = types.get(report_type)
        if isinstance(item, dict):
            return item
    return {}


def report_type_enabled(cfg: dict[str, Any], report_type: str) -> bool:
    item = report_type_config(cfg, report_type)
    if item:
        return bool(item.get("enabled", True))
    return report_type == REPORT_SANDBOX_STATUS


def report_interval_seconds(cfg: dict[str, Any], report_type: str = REPORT_SANDBOX_STATUS) -> int:
    report_cfg = cfg.get("report", {}) or {}
    item = report_type_config(cfg, report_type)
    raw = item.get("interval_seconds") if "interval_seconds" in item else report_cfg.get("interval_seconds", 60)
    try:
        return int(raw)
    except Exception:
        return 60


def report_endpoint(cfg: dict[str, Any], report_type: str) -> str:
    report_cfg = cfg.get("report", {}) or {}
    item = report_type_config(cfg, report_type)
    explicit = str(item.get("endpoint", "") or "").strip()
    if explicit:
        return explicit
    path = str(item.get("path", "") or "").strip()
    base = report_base_url(cfg)
    if path and base:
        return f"{base}{path if path.startswith('/') else '/' + path}"
    if report_type == REPORT_SANDBOX_STATUS:
        return str(report_cfg.get("endpoint", "") or "").strip()
    return ""


def append_report_request(
    cfg: dict[str, Any],
    report_type: str = REPORT_SANDBOX_STATUS,
    reason: str = "",
    payload: dict[str, Any] | None = None,
) -> str | None:
    if not bool((cfg.get("report", {}) or {}).get("enabled", False)):
        return None
    normalized = _normalize_report_type(report_type)
    if not normalized or not report_type_enabled(cfg, normalized):
        log("warn", "report.request.unsupported", "运行态上报类型未启用或不支持", report_type=str(report_type or ""))
        return None
    request_payload = {
        "requestId": str(uuid.uuid4()),
        "reportType": normalized,
        "reason": str(reason or ""),
        "createdAtMs": now_ms(),
        **({"payload": payload} if isinstance(payload, dict) else {}),
    }
    path = report_request_file(cfg)
    lock_path = report_lock_file(cfg)
    try:
        last_exc: Exception | None = None
        for _ in range(5):
            try:
                with FileLock(lock_path):
                    ensure_parent(path)
                    with open(path, "a", encoding="utf-8") as fp:
                        fp.write(json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                time.sleep(0.05)
        if last_exc is not None:
            raise last_exc
        log("info", "report.request.appended", "运行态上报请求已追加", report_type=request_payload["reportType"], reason=request_payload["reason"])
        return normalized
    except Exception as exc:
        log("warn", "report.request.append_failed", "运行态上报请求追加失败", reason=reason, error=str(exc))
        return None


def _read_request_lines(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        return p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []


def _consume_request_lines(cfg: dict[str, Any]) -> list[str]:
    path = report_request_file(cfg)
    with FileLock(report_lock_file(cfg)):
        lines = _read_request_lines(path)
        if lines:
            _clear_requests(path)
        return lines


def _request_dedupe_key(raw: dict[str, Any]) -> str:
    report_type = _normalize_report_type(raw.get("reportType"))
    if not report_type:
        return ""
    if report_type == REPORT_UPGRADE_FAILED:
        payload = raw.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        process_name = str(payload.get("processName") or "").strip()
        return f"{report_type}:{process_name}" if process_name else report_type
    return report_type


def _request_dedupe_key_from_line(line: str) -> str:
    try:
        raw = json.loads(str(line or "").strip() or "{}")
    except Exception:
        return ""
    if not isinstance(raw, dict):
        return ""
    return _request_dedupe_key(raw)


def _restore_latest_request_per_key(cfg: dict[str, Any], consumed_lines: list[str]) -> None:
    if not consumed_lines:
        return
    latest_by_key: dict[str, str] = {}
    for line in consumed_lines:
        key = _request_dedupe_key_from_line(line)
        if key:
            latest_by_key[key] = line
    if not latest_by_key:
        return
    path = report_request_file(cfg)
    with FileLock(report_lock_file(cfg)):
        current = _read_request_lines(path)
        current_keys = {_request_dedupe_key_from_line(line) for line in current}
        restored = [line for key, line in latest_by_key.items() if key not in current_keys]
        if not restored:
            return
        ensure_parent(path)
        payload = "\n".join(restored + current)
        Path(path).write_text((payload + "\n") if payload else "", encoding="utf-8")


def _parse_requests(lines: list[str]) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            requests.append(raw)
    return requests


def _clear_requests(path: str) -> None:
    ensure_parent(path)
    Path(path).write_text("", encoding="utf-8")


def resolve_sandbox_id(cfg: dict[str, Any]) -> str:
    direct = os.environ.get("X_SANDBOX_ID", "").strip()
    if direct:
        return direct
    cached = str(read_env_cache(cfg).get("X_SANDBOX_ID", "")).strip()
    if cached:
        return cached
    return os.environ.get("DAYTONA_SANDBOX_ID", "").strip()


def _resolve_env_value(cfg: dict[str, Any], key: str) -> str:
    value = os.environ.get(key, "").strip()
    if value:
        return value
    return str(read_env_cache(cfg).get(key, "")).strip()


def _read_text_file(path: str) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    p = Path(text)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except Exception:
        return ""


def _process_version(proc: dict[str, Any]) -> str:
    upgrade = proc.get("upgrade", {}) or {}
    return _read_text_file(str(upgrade.get("current_version_file", "")))


def _number_or_unknown(value: Any) -> float:
    try:
        if value is None:
            return -1
        return round(float(value), 2)
    except Exception:
        return -1


def _int_or_unknown(value: Any) -> int:
    try:
        if value is None:
            return -1
        return int(value)
    except Exception:
        return -1


def _time_ms_or_zero(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except Exception:
        return 0


def _process_report_enabled(proc: dict[str, Any]) -> bool:
    report_cfg = proc.get("report", {}) or {}
    return isinstance(report_cfg, dict) and bool(report_cfg.get("enabled", False))


def _process_health_state(rec: dict[str, Any] | None) -> int:
    if not isinstance(rec, dict):
        return -1
    status = str(rec.get("status") or "").strip()
    raw = str(rec.get("manager_raw_status") or "").strip().upper()
    message = str(rec.get("message") or "").lower()
    if status == "healthy":
        return 1
    if raw in {"ERROR", "DUPLICATE"}:
        return -1 if raw == "ERROR" else 0
    if status in {"failed", "recovering"}:
        return 0
    if status in {"upgrading", "disabled"}:
        return -1
    if "startup grace" in message:
        return -1
    return -1


def _process_report_item(proc: dict[str, Any], state_processes: dict[str, Any]) -> dict[str, Any]:
    name = str(proc.get("name", "")).strip()
    rec = state_processes.get(name, {}) if isinstance(state_processes, dict) else {}
    if not isinstance(rec, dict):
        rec = {}
    update_result = _int_or_unknown(rec.get("last_update_result"))
    return {
        "processName": name,
        "processVersion": _process_version(proc),
        "processLastUpdateTime": _time_ms_or_zero(rec.get("last_update_time_ms")),
        "processUptime": _int_or_unknown(rec.get("runtime_seconds")),
        "processLastUpdateResult": update_result,
        "processUpdateResult": update_result,
        "updateTargetVersion": str(rec.get("last_update_target_version") or ""),
        "processHealthState": _process_health_state(rec),
    }


def _guard_report_state(prev: dict[str, Any], now: int) -> dict[str, Any]:
    prev_guard = prev.get("guard_report") if isinstance(prev.get("guard_report"), dict) else {}
    out = {
        "last_update_time_ms": _time_ms_or_zero(prev_guard.get("last_update_time_ms")),
        "last_update_result": _int_or_unknown(prev_guard.get("last_update_result")),
        "last_update_target_version": str(prev_guard.get("last_update_target_version") or ""),
    }
    previous_version = str(prev.get("daemon_version") or "").strip()
    if previous_version and previous_version != APP_VERSION:
        out["last_update_time_ms"] = now
        out["last_update_result"] = 1
        out["last_update_target_version"] = APP_VERSION
    return out


def _guard_report_item(guard_state: dict[str, Any], now: int) -> dict[str, Any]:
    update_result = _int_or_unknown(guard_state.get("last_update_result"))
    return {
        "processName": "guard",
        "processVersion": APP_VERSION,
        "processLastUpdateTime": _time_ms_or_zero(guard_state.get("last_update_time_ms")),
        "processUptime": max(0, int((now - DAEMON_STARTED_AT_MS) / 1000)),
        "processLastUpdateResult": update_result,
        "processUpdateResult": update_result,
        "updateTargetVersion": str(guard_state.get("last_update_target_version") or ""),
        "processHealthState": 1,
    }


def build_sandbox_status_payload(
    cfg: dict[str, Any],
    state: dict[str, Any],
    prev: dict[str, Any] | None = None,
    request: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    sandbox_id = resolve_sandbox_id(cfg)
    if not sandbox_id:
        return None
    now = now_ms()
    state_resources = state.get("resources") if isinstance(state.get("resources"), dict) else {}
    disk_used_percent = state_resources.get("diskUsedPercent")
    if disk_used_percent is None:
        disk_used_percent = current_disk_used_percent()
    resources = {
        "diskUtilization": _number_or_unknown(disk_used_percent),
        "memoryUtilization": _number_or_unknown(state_resources.get("memoryUsedPercent")),
        "cpuUtilization": _number_or_unknown(state_resources.get("cpuUsedPercent")),
    }
    guard_state = _guard_report_state(prev or {}, now)
    state["guard_report"] = guard_state
    state_processes = state.get("processes", {}) if isinstance(state.get("processes"), dict) else {}
    processes: list[dict[str, Any]] = [_guard_report_item(guard_state, now)]
    for proc in cfg.get("processes", []) or []:
        if isinstance(proc, dict) and process_is_applicable(proc, cfg) and _process_report_enabled(proc):
            processes.append(_process_report_item(proc, state_processes))
    xagent = next((item for item in processes if item.get("processName") == "xagent"), None)
    sandbox_health_state = int(xagent.get("processHealthState")) if isinstance(xagent, dict) else -1
    payload: dict[str, Any] = {
        "sandboxId": sandbox_id,
        "reportType": REPORT_SANDBOX_STATUS,
        "timeUnixMs": now,
        "sandboxType": _resolve_env_value(cfg, "X_SANDBOX_TYPE"),
        "sandboxPlatform": _resolve_env_value(cfg, "X_SANDBOX_PLATFORM"),
        "sandboxHealthState": sandbox_health_state,
        "resources": resources,
        "processes": processes,
    }
    return payload


def build_upgrade_failed_payload(
    cfg: dict[str, Any],
    state: dict[str, Any],
    prev: dict[str, Any] | None = None,
    request: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    sandbox_id = resolve_sandbox_id(cfg)
    if not sandbox_id:
        return None
    request = request or {}
    extra = request.get("payload", {})
    if not isinstance(extra, dict):
        extra = {}
    failed_at = _time_ms_or_zero(extra.get("failedAtMs") or request.get("createdAtMs") or now_ms())
    return {
        "sandboxId": sandbox_id,
        "reportType": REPORT_UPGRADE_FAILED,
        "processName": str(extra.get("processName") or ""),
        "targetVersion": str(extra.get("targetVersion") or ""),
        "failureReason": str(extra.get("failureReason") or "升级失败"),
        "failedAtMs": failed_at,
    }


def build_startup_timing_payload(
    cfg: dict[str, Any],
    state: dict[str, Any],
    prev: dict[str, Any] | None = None,
    request: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    sandbox_id = resolve_sandbox_id(cfg)
    if not sandbox_id:
        return None
    metrics = build_startup_timing_metrics(read_startup_timing(cfg))
    if metrics is None:
        return None
    return {
        "reportType": REPORT_STARTUP_TIMING,
        "sandboxId": sandbox_id,
        "sandboxType": _resolve_env_value(cfg, "X_SANDBOX_TYPE"),
        "sandboxPlatform": _resolve_env_value(cfg, "X_SANDBOX_PLATFORM"),
        "timeUnixMs": now_ms(),
        **metrics,
    }


def build_report_payload(
    cfg: dict[str, Any],
    state: dict[str, Any],
    prev: dict[str, Any] | None = None,
    report_type: str = REPORT_SANDBOX_STATUS,
    request: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    normalized = _normalize_report_type(report_type)
    if normalized == REPORT_SANDBOX_STATUS:
        return build_sandbox_status_payload(cfg, state, prev, request)
    if normalized == REPORT_UPGRADE_FAILED:
        return build_upgrade_failed_payload(cfg, state, prev, request)
    if normalized == REPORT_STARTUP_TIMING:
        return build_startup_timing_payload(cfg, state, prev, request)
    return None


def _has_payload_builder(report_type: str) -> bool:
    return report_type in {REPORT_SANDBOX_STATUS, REPORT_UPGRADE_FAILED, REPORT_STARTUP_TIMING}


def _is_due(prev_type_report: dict[str, Any], interval_seconds: int) -> bool:
    if interval_seconds < 0:
        return False
    due_at = prev_type_report.get("next_report_due_at_ms")
    if due_at is not None:
        try:
            return now_ms() >= int(due_at)
        except Exception:
            return True
    try:
        last = int(prev_type_report.get("last_report_at_ms") or 0)
    except Exception:
        last = 0
    return last <= 0 or now_ms() - last >= interval_seconds * 1000


def _empty_type_state(prev_report: dict[str, Any], report_type: str) -> dict[str, Any]:
    prev_types = prev_report.get("types") if isinstance(prev_report.get("types"), dict) else {}
    prev_type = prev_types.get(report_type, {}) if isinstance(prev_types, dict) else {}
    if not isinstance(prev_type, dict):
        prev_type = {}
    if report_type == REPORT_SANDBOX_STATUS and not prev_type:
        prev_type = prev_report
    return {
        "status": prev_type.get("status", "ok"),
        "last_report_at_ms": prev_type.get("last_report_at_ms"),
        "next_report_due_at_ms": prev_type.get("next_report_due_at_ms") if report_type == REPORT_SANDBOX_STATUS else None,
        "last_error": "",
    }


def _initial_report_state(report_cfg: dict[str, Any], prev_report: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(report_cfg.get("enabled", False))
    type_state = {report_type: _empty_type_state(prev_report, report_type) for report_type in KNOWN_REPORT_TYPES}
    return {
        "enabled": enabled,
        "status": "disabled" if not enabled else "ok",
        "last_report_type": prev_report.get("last_report_type"),
        "last_report_at_ms": prev_report.get("last_report_at_ms"),
        "next_report_due_at_ms": type_state[REPORT_SANDBOX_STATUS].get("next_report_due_at_ms"),
        "last_error": "",
        "pending_request_count": 0,
        "types": type_state,
    }


def _periodic_status_request(cfg: dict[str, Any], prev_report: dict[str, Any]) -> dict[str, Any] | None:
    if not report_type_enabled(cfg, REPORT_SANDBOX_STATUS):
        return None
    prev_type = _empty_type_state(prev_report, REPORT_SANDBOX_STATUS)
    interval = report_interval_seconds(cfg, REPORT_SANDBOX_STATUS)
    if not _is_due(prev_type, interval):
        return None
    return {
        "requestId": "periodic",
        "reportType": REPORT_SANDBOX_STATUS,
        "reason": "periodic",
        "createdAtMs": now_ms(),
        "_periodic": True,
    }


def _selected_requests(cfg: dict[str, Any], prev_report: dict[str, Any], requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    has_status_request = False
    for request in requests:
        report_type = _normalize_report_type(request.get("reportType"))
        if not report_type or not report_type_enabled(cfg, report_type):
            continue
        if report_type == REPORT_SANDBOX_STATUS:
            has_status_request = True
            continue
        selected.append({**request, "reportType": report_type})
    if has_status_request:
        selected.append(
            {
                "requestId": "manual-status",
                "reportType": REPORT_SANDBOX_STATUS,
                "reason": "manual",
                "createdAtMs": now_ms(),
            }
        )
    periodic = _periodic_status_request(cfg, prev_report)
    if periodic and not has_status_request:
        selected.append(periodic)
    return selected


def reconcile_report(cfg: dict[str, Any], state: dict[str, Any], prev: dict[str, Any]) -> None:
    report_cfg = cfg.get("report", {}) or {}
    prev_report = dict(prev.get("report", {}) or {})
    lines: list[str] = []
    requests: list[dict[str, Any]] = []
    report_state = _initial_report_state(report_cfg, prev_report)
    state["report"] = report_state
    if not bool(report_cfg.get("enabled", False)):
        return
    try:
        lines = _consume_request_lines(cfg)
        requests = _parse_requests(lines)
        report_state["pending_request_count"] = len(requests)
    except Exception as exc:
        report_state["status"] = "degraded"
        report_state["last_error"] = f"consume request failed: {exc}"
        log("warn", "report.request.consume_failed", "运行态上报 request 读取失败", error=str(exc))
        return

    selected = _selected_requests(cfg, prev_report, requests)
    if not selected:
        return

    any_success = False
    for request in selected:
        report_type = str(request.get("reportType") or "")
        type_state = report_state["types"].setdefault(report_type, _empty_type_state(prev_report, report_type))
        if not _has_payload_builder(report_type):
            type_state["status"] = "skipped"
            type_state["last_error"] = "payload builder missing"
            log("warn", "report.builder_missing", "运行态上报 payload builder 缺失", report_type=report_type)
            continue
        payload = build_report_payload(cfg, state, prev, report_type, request)
        if payload is None:
            report_state["status"] = "skipped"
            report_state["last_error"] = "payload_not_ready" if report_type == REPORT_STARTUP_TIMING else "sandbox_id_missing"
            type_state["status"] = "skipped"
            type_state["last_error"] = report_state["last_error"]
            if report_type == REPORT_STARTUP_TIMING:
                try:
                    _restore_latest_request_per_key(
                        cfg,
                        [line for line in lines if _request_dedupe_key_from_line(line) == REPORT_STARTUP_TIMING],
                    )
                except Exception as exc:
                    report_state["status"] = "degraded"
                    report_state["last_error"] = f"restore request failed: {exc}"
                continue
            try:
                _restore_latest_request_per_key(cfg, lines)
            except Exception as exc:
                report_state["status"] = "degraded"
                report_state["last_error"] = f"restore request failed: {exc}"
            return
        endpoint = report_endpoint(cfg, report_type)
        if not endpoint:
            report_state["status"] = "degraded"
            report_state["last_error"] = f"endpoint missing for {report_type}"
            type_state["status"] = "degraded"
            type_state["last_error"] = "endpoint missing"
            log("warn", "report.endpoint_missing", "运行态上报 endpoint 缺失", report_type=report_type)
            continue
        try:
            timeout = float(report_cfg.get("timeout_seconds", 3))
            http_post_json_no_response(endpoint, payload, timeout_seconds=timeout)
        except Exception as exc:
            log("info", "report.send", "运行态上报已发起", report_type=report_type, endpoint=endpoint, error=str(exc))
        else:
            log("info", "report.send", "运行态上报已发起", report_type=report_type, endpoint=endpoint)

        sent_at = _time_ms_or_zero(payload.get("timeUnixMs") or payload.get("failedAtMs") or now_ms())
        any_success = True
        type_state["status"] = "ok"
        type_state["last_report_at_ms"] = sent_at
        type_state["last_error"] = ""
        if report_type == REPORT_SANDBOX_STATUS:
            interval = report_interval_seconds(cfg, REPORT_SANDBOX_STATUS)
            type_state["next_report_due_at_ms"] = sent_at + interval * 1000 if interval >= 0 else 0
            report_state["next_report_due_at_ms"] = type_state["next_report_due_at_ms"]
        elif report_type == REPORT_STARTUP_TIMING:
            mark_startup_timing_reported(cfg)
        report_state["last_report_type"] = report_type
        report_state["last_report_at_ms"] = sent_at

    if any_success and report_state["status"] != "degraded":
        report_state["status"] = "ok"
    report_state["pending_request_count"] = 0
