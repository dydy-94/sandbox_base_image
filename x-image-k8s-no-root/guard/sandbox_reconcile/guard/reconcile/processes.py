from __future__ import annotations

"""进程巡检与恢复/升级调度。"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import time
from typing import Any

from ..common import log, now_iso, shlex_quote
from ..env_store import load_env_requests, remove_consumed_env_requests
from ..process_applicability import inapplicable_process_state, process_is_applicable
from ..runtime_profile import is_rootless_profile
from ..strategy.factory import get_strategy
from ..upgrade import load_upgrade_events, load_upgrade_requests, schedule_upgrade
from ..types import CommandResult
from ..xagent_sessions import probe_xagent_running_sessions
from ..xagent_heartbeat_control import disable_heartbeat_before_process_stop

XAGENT_IDLE_PENDING_REASON = "xagent_idle_gate"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_to_ms(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _parse_version_probe(proc: dict[str, Any], run_command_func) -> str | None:
    """读取进程当前版本，优先兼容顶层 version_probe，再复用 upgrade 版本配置。"""

    def _first_line(text: str) -> str | None:
        return (text.strip().splitlines() or [""])[0].strip() or None

    probe = proc.get("version_probe")
    if isinstance(probe, dict):
        t = probe.get("type")
        if t == "command" and probe.get("command"):
            res = run_command_func(str(probe["command"]), timeout=10)
            if res.returncode == 0:
                version = _first_line(res.stdout)
                if version:
                    return version
        if t == "file" and probe.get("file_path"):
            p = Path(str(probe["file_path"]))
            if p.exists():
                try:
                    version = _first_line(p.read_text(encoding="utf-8"))
                    if version:
                        return version
                except Exception:
                    pass

    upgrade = proc.get("upgrade", {}) or {}
    current_version_command = str(upgrade.get("current_version_command", "")).strip()
    if current_version_command:
        res = run_command_func(current_version_command, timeout=10)
        if res.returncode == 0:
            version = _first_line(res.stdout)
            if version:
                return version
    current_version_file = str(upgrade.get("current_version_file", "")).strip()
    if current_version_file:
        p = Path(current_version_file)
        if p.exists():
            try:
                return _first_line(p.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def _status_from_probe(running: bool, transitional: bool, required: bool, recovering: bool) -> str:
    """将探测结果映射为统一状态枚举。"""
    if running:
        return "healthy"
    if recovering:
        return "recovering"
    if not required:
        return "disabled"
    if transitional:
        return "recovering"
    return "failed"


def _is_action_ok(manager: str, result: CommandResult) -> bool:
    """判断动作执行是否成功（包含幂等成功语义）。"""
    if result.returncode == 0:
        return True
    text = f"{result.stdout}\n{result.stderr}".lower()
    if manager == "supervisor" and "already started" in text:
        return True
    if manager == "pm2" and "already launched" in text:
        return True
    return False


def _daemon_policy(proc: dict[str, Any], manager: str) -> str:
    """返回守护策略。

    - ensure_exists: 仅保证进程存在（适用于 PM2 自愈场景）
    - ensure_running: 保证进程运行中（适用于 supervisor）
    """
    policy = str(proc.get("daemon_policy") or "").strip()
    if policy in {"ensure_exists", "ensure_running"}:
        return policy
    if manager == "pm2":
        return "ensure_exists"
    return "ensure_running"


def _is_meta_package_upgrade(upgrade_cfg: dict[str, Any]) -> bool:
    return bool(upgrade_cfg.get("enabled", False)) and str(upgrade_cfg.get("strategy", "")).strip() == "meta_package"


def _is_upgrade_enabled(upgrade_cfg: dict[str, Any]) -> bool:
    return bool(upgrade_cfg.get("enabled", False)) and str(upgrade_cfg.get("strategy", "")).strip() in {
        "code_server_package",
        "meta_package",
        "xagent_package",
    }


def _bootstrap_should_schedule_upgrade(upgrade_cfg: dict[str, Any]) -> bool:
    return bool(upgrade_cfg.get("bootstrap_schedule", True))


def _upgrade_dependencies(upgrade_cfg: dict[str, Any]) -> list[str]:
    raw = upgrade_cfg.get("defer_until_processes_stable", [])
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    deps: list[str] = []
    for item in raw:
        name = str(item).strip()
        if name and name not in deps:
            deps.append(name)
    return deps


def _upgrade_dependencies_stable(upgrade_cfg: dict[str, Any], effective_states: dict[str, str]) -> bool:
    for dep in _upgrade_dependencies(upgrade_cfg):
        if str(effective_states.get(dep, "stable")).strip() != "stable":
            return False
    return True


def _defer_recovery_for_active_upgrades(
    proc: dict[str, Any],
    effective_states: dict[str, str],
    *,
    any_upgrading: bool,
) -> bool:
    raw = proc.get("defer_recover_while_upgrading", False)
    if isinstance(raw, bool):
        return raw and any_upgrading
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return False
    blockers = {str(item).strip() for item in raw if str(item).strip()}
    return any(
        name in blockers and str(state).strip() in {"pending", "upgrading"}
        for name, state in effective_states.items()
    )


def _is_xagent_package(proc: dict[str, Any]) -> bool:
    return str(((proc.get("upgrade", {}) or {}).get("strategy", ""))).strip() == "xagent_package"


def _is_xagent_idle_pending(proc_state: dict[str, Any]) -> bool:
    return str(proc_state.get("upgrade_pending_reason", "")).strip() == XAGENT_IDLE_PENDING_REASON


def _is_pm2_delete_not_found(result: CommandResult) -> bool:
    text = f"{result.stdout}\n{result.stderr}".lower()
    return any(marker in text for marker in ["not found", "does not exist", "doesn't exist", "process or namespace not found"])


def _is_stop_action_ok(manager: str, result: CommandResult) -> bool:
    if result.returncode == 0:
        return True
    text = f"{result.stdout}\n{result.stderr}".lower()
    if manager == "pm2":
        return _is_pm2_delete_not_found(result)
    if manager == "supervisor":
        return any(marker in text for marker in ["not running", "not started", "already stopped"])
    return False


def _reconcile_inapplicable_process(
    proc: dict[str, Any],
    cfg: dict[str, Any],
    prev_proc: dict[str, Any],
) -> dict[str, Any]:
    """确保不适用进程停止，同时保留安装文件和升级历史。"""
    rec = inapplicable_process_state(proc, cfg)
    rec["last_check_time"] = now_iso()
    rec["last_update_time_ms"] = prev_proc.get("last_update_time_ms")
    rec["last_update_result"] = prev_proc.get("last_update_result")
    rec["last_update_target_version"] = prev_proc.get("last_update_target_version")
    manager = str(proc.get("manager", "pm2")).strip() or "pm2"
    try:
        strategy = get_strategy(manager)
    except ValueError as exc:
        rec["manager_capability"] = "unsupported"
        rec["message"] = str(exc)
        return rec

    probe = strategy.probe(proc, cfg)
    raw_status = str(probe.raw_status).strip()
    rec["manager_raw_status"] = raw_status
    rec["manager_message"] = probe.message
    if raw_status == "ERROR":
        if manager == "pm2" and not probe.exists and probe.message == "pm2 daemon not ready":
            rec["message"] = "process not applicable and PM2 daemon is not running"
            return rec
        rec["message"] = f"sandbox selector disabled process; manager probe failed: {probe.message}"
        log(
            "warn",
            "process.inapplicable_probe_failed",
            "不适用进程状态探测失败，下一轮继续确认停止状态",
            process=str(proc.get("name", "")),
            manager=manager,
            error=probe.message,
        )
        return rec

    should_stop = probe.exists if manager == "pm2" else (
        probe.running or (probe.transitional and raw_status.upper() != "STOPPING")
    )
    if not should_stop:
        rec["message"] = "process not applicable and already stopped"
        return rec

    unregister_result = disable_heartbeat_before_process_stop(
        cfg,
        str(proc.get("name", "")),
    )
    if unregister_result is not None:
        rec["heartbeat_unregister_result"] = "success" if unregister_result else "failed"
    result = strategy.stop(proc, cfg)
    rec["last_action"] = "stop"
    rec["last_action_at"] = now_iso()
    if _is_stop_action_ok(manager, result):
        rec["last_action_result"] = "success"
        rec["message"] = "process stopped because sandbox selector does not match"
        log(
            "info",
            "process.stopped_inapplicable",
            "进程不适用于当前沙箱，已停止进程并保留安装文件",
            process=str(proc.get("name", "")),
            manager=manager,
        )
    else:
        rec["last_action_result"] = "failed"
        rec["message"] = result.stderr.strip() or result.stdout.strip() or "stop process failed"
        log(
            "warn",
            "process.stop_inapplicable_failed",
            "停止不适用进程失败，下一轮继续重试",
            process=str(proc.get("name", "")),
            manager=manager,
            returncode=result.returncode,
            error=rec["message"],
        )
    return rec


def _xagent_env_request_ids(requests: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for req in requests:
        if str(req.get("type", "")).strip() != "xagent_env_changed":
            continue
        rid = str(req.get("request_id", "")).strip()
        if rid:
            ids.add(rid)
    return ids


def _effective_upgrade_states(
    cfg_processes: list[dict[str, Any]],
    prev_processes: dict[str, Any],
    event_map: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """基于上一轮状态和本轮 upgrade 事件，计算本轮有效的 upgrade_state 视图。"""
    states: dict[str, str] = {}
    for proc in cfg_processes:
        if not isinstance(proc, dict):
            continue
        name = str(proc.get("name", "")).strip()
        if not name:
            continue
        prev_proc = (prev_processes or {}).get(name, {})
        state = str((prev_proc or {}).get("upgrade_state", "stable")).strip() or "stable"
        if state == "pending" and _is_xagent_idle_pending(prev_proc or {}):
            state = "stable"
        ev = event_map.get(name)
        if isinstance(ev, dict):
            state = "stable" if ev.get("ok") else "upgrade_failed"
        states[name] = state
    return states


def _run_pre_recover_command(proc: dict[str, Any], run_command_func) -> CommandResult | None:
    """在恢复动作前执行预处理命令。

    适用于删除锁文件、清理残留状态等场景。
    未配置时返回 None。
    """
    cmd = str(proc.get("pre_recover_command", "")).strip()
    if not cmd:
        return None
    return run_command_func(cmd, timeout=30)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts))
    except Exception:
        return None


def _retry_delay_seconds(retry_count: int, base_delay: int, max_delay: int) -> int:
    step = max(0, retry_count - 1)
    return min(base_delay * (2**step), max_delay)


def _is_disk_insufficient_upgrade_error(error: Any) -> bool:
    text = str(error or "").strip().lower()
    return "insufficient free disk" in text or "no space left" in text or "磁盘" in text


def _is_stale_upgrading(prev_proc: dict[str, Any], timeout_seconds: int = 60) -> bool:
    """判断 upgrading 状态是否已陈旧且可回退到直接巡检。"""
    started_at = _parse_iso(str(prev_proc.get("meta_last_check_at") or prev_proc.get("last_action_at") or ""))
    if started_at is None:
        return True
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started_at).total_seconds() > timeout_seconds


def _clear_upgrade_idle_fields(rec: dict[str, Any]) -> None:
    rec["upgrade_pending_reason"] = None
    rec["upgrade_idle_started_at"] = None
    rec["upgrade_force_after"] = None
    rec["upgrade_idle_last_probe_at"] = None
    rec["upgrade_idle_last_status"] = None
    rec["upgrade_idle_last_error"] = None


def _xagent_idle_gate(upgrade_cfg: dict[str, Any]) -> dict[str, Any] | None:
    gate = upgrade_cfg.get("idle_gate")
    if not isinstance(gate, dict) or not bool(gate.get("enabled", False)):
        return None
    url = str(gate.get("url", "")).strip()
    if not url:
        return None
    return gate


def _xagent_idle_force_after(started_at: str, max_wait_seconds: int) -> str | None:
    if max_wait_seconds < 0:
        return None
    started = _parse_iso(started_at)
    if started is None:
        started = datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (started + timedelta(seconds=max_wait_seconds)).isoformat()


def _xagent_idle_force_due(force_after: str | None) -> bool:
    force_at = _parse_iso(force_after)
    if force_at is None:
        return False
    if force_at.tzinfo is None:
        force_at = force_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= force_at


def _should_defer_xagent_upgrade_for_idle(
    proc: dict[str, Any],
    rec: dict[str, Any],
    prev_proc: dict[str, Any],
    target_version: str,
) -> bool:
    """返回 True 表示本轮仅记录 idle pending，不启动 upgrade-runner。"""
    if not _is_xagent_package(proc):
        return False
    upgrade_cfg = proc.get("upgrade", {}) or {}
    gate = _xagent_idle_gate(upgrade_cfg)
    if gate is None:
        return False

    try:
        max_wait_seconds = int(gate.get("max_wait_seconds", 600))
    except Exception:
        max_wait_seconds = 600
    fail_open = bool(gate.get("fail_open", True))
    now = now_iso()
    started_at = str(
        rec.get("upgrade_idle_started_at")
        or prev_proc.get("upgrade_idle_started_at")
        or now
    )
    force_after = rec.get("upgrade_force_after") or prev_proc.get("upgrade_force_after")
    if force_after is None:
        force_after = _xagent_idle_force_after(started_at, max_wait_seconds)

    rec["upgrade_state"] = "pending"
    rec["pending_target_version"] = target_version
    rec["upgrade_pending_reason"] = XAGENT_IDLE_PENDING_REASON
    rec["upgrade_idle_started_at"] = started_at
    rec["upgrade_force_after"] = force_after
    rec["upgrade_idle_last_probe_at"] = now
    rec["last_action"] = "upgrade"
    rec["last_action_result"] = "skipped"
    rec["last_action_at"] = now
    rec["meta_last_check_at"] = now

    if _xagent_idle_force_due(str(force_after or "")):
        rec["upgrade_idle_last_status"] = "force_due"
        rec["upgrade_idle_last_error"] = None
        log(
            "warn",
            "daemon.xagent_idle.force_due",
            "xagent 空闲等待超过最大时长，强制执行升级",
            process=str(proc.get("name", "xagent")),
            target_version=target_version,
            force_after=force_after,
        )
        return False

    ok, total, error = probe_xagent_running_sessions(gate)
    if not ok:
        rec["upgrade_idle_last_status"] = "check_failed"
        rec["upgrade_idle_last_error"] = error
        log(
            "warn",
            "daemon.xagent_idle.check_failed",
            "xagent 空闲接口调用失败",
            process=str(proc.get("name", "xagent")),
            target_version=target_version,
            error=error,
            fail_open=fail_open,
        )
        if fail_open:
            return False
        rec["message"] = f"xagent idle check failed; upgrade pending: {error}"
        return True

    rec["upgrade_idle_last_error"] = None
    if int(total or 0) == 0:
        rec["upgrade_idle_last_status"] = "ready"
        log(
            "info",
            "daemon.xagent_idle.ready",
            "xagent 当前空闲，允许执行升级",
            process=str(proc.get("name", "xagent")),
            target_version=target_version,
        )
        return False

    rec["upgrade_idle_last_status"] = "busy"
    rec["message"] = f"xagent busy; upgrade pending until idle: running_sessions={total}"
    log(
        "info",
        "daemon.xagent_idle.busy",
        "xagent 当前存在运行中 session，暂缓升级",
        process=str(proc.get("name", "xagent")),
        target_version=target_version,
        running_sessions=total,
        force_after=force_after,
    )
    return True


def _run_health_check(proc: dict[str, Any], run_command_func) -> tuple[bool, str]:
    """执行可选健康探针。

    当前支持最小版 http_json：
    - 通过 curl 请求 URL
    - 解析 JSON
    - 校验指定字段值
    """
    health = proc.get("health_check")
    if not isinstance(health, dict):
        return True, ""

    health_type = str(health.get("type", "")).strip()
    if health_type != "http_json":
        return True, ""

    method = str(health.get("method", "GET")).strip().upper() or "GET"
    if method not in {"GET", "POST"}:
        return False, f"unsupported health probe method: {method}"
    url = str(health.get("url", "")).strip()
    accept_any_json = health.get("accept_any_json", False) is True
    expect_field = str(health.get("expect_json_field", "")).strip()
    expect_value = health.get("expect_json_value", "")
    expect_array_contains = health.get("expect_json_array_contains")
    timeout_seconds = max(1, int(health.get("timeout_seconds", 3)))
    if not url or (
        not accept_any_json
        and not expect_field
        and not isinstance(expect_array_contains, dict)
    ):
        return True, ""

    cmd_parts = ["curl", "-fsS", "--max-time", str(timeout_seconds)]
    if method != "GET":
        cmd_parts.extend(["-X", method])
    headers = health.get("headers")
    header_keys: set[str] = set()
    if isinstance(headers, dict):
        for key, value in headers.items():
            header_key = str(key).strip()
            if not header_key:
                continue
            header_keys.add(header_key.lower())
            cmd_parts.extend(["-H", f"{header_key}: {value}"])
    if "body_json" in health:
        body = json.dumps(health.get("body_json"), ensure_ascii=False, separators=(",", ":"))
        if "content-type" not in header_keys:
            cmd_parts.extend(["-H", "Content-Type: application/json"])
        cmd_parts.extend(["-d", body])
    elif "body" in health:
        cmd_parts.extend(["-d", str(health.get("body", ""))])
    cmd_parts.append(url)
    cmd = " ".join(shlex_quote(part) for part in cmd_parts)
    res = run_command_func(cmd, timeout=timeout_seconds + 1)
    if res.returncode != 0:
        return False, res.stderr.strip() or res.stdout.strip() or "health probe request failed"
    try:
        payload = json.loads(res.stdout.strip() or "{}")
    except Exception:
        return False, "health probe response is not valid json"
    if accept_any_json:
        return True, "health probe ok"
    if isinstance(expect_array_contains, dict):
        if not isinstance(payload, list):
            return False, "health probe response json must be array"
        for item in payload:
            if not isinstance(item, dict):
                continue
            matched = True
            for key, expected in expect_array_contains.items():
                actual = item.get(str(key))
                if actual != expected and str(actual) != str(expected):
                    matched = False
                    break
            if matched:
                return True, "health probe ok"
        return False, f"health probe array does not contain {expect_array_contains!r}"
    if not isinstance(payload, dict):
        return False, "health probe response json must be object"
    actual = payload.get(expect_field)
    if actual != expect_value and str(actual) != str(expect_value):
        return False, f"health probe {expect_field}={actual!r}, expect={expect_value!r}"
    return True, "health probe ok"


def _health_failure_threshold(proc: dict[str, Any]) -> int:
    """返回健康探针失败阈值；未配置或无 health_check 时返回 0。"""
    if not isinstance(proc.get("health_check"), dict):
        return 0
    try:
        threshold = int(proc.get("health_failure_threshold", 0))
    except Exception:
        return 0
    return max(0, threshold)


def _stability_policy(proc: dict[str, Any], manager: str) -> dict[str, Any] | None:
    """返回进程稳定性恢复策略；未显式启用时不介入现有恢复语义。"""
    policy = proc.get("stability_policy")
    if manager != "pm2" or not isinstance(policy, dict) or not bool(policy.get("enabled", False)):
        return None
    return policy


def _stability_int(policy: dict[str, Any], key: str, default: int) -> int:
    try:
        return max(0, int(policy.get(key, default)))
    except Exception:
        return default


def _stability_backoff_seconds(policy: dict[str, Any], recovery_count: int) -> int:
    raw = policy.get("backoff_seconds", [0, 30, 120, 600, 1800])
    if not isinstance(raw, list) or not raw:
        raw = [0, 30, 120, 600, 1800]
    values: list[int] = []
    for item in raw:
        try:
            values.append(max(0, int(item)))
        except Exception:
            continue
    if not values:
        values = [0, 30, 120, 600, 1800]
    idx = min(max(0, recovery_count - 1), len(values) - 1)
    return values[idx]


def _clear_stability_backoff(rec: dict[str, Any]) -> None:
    rec["stability_failure_version"] = None
    rec["stability_recovery_count"] = 0
    rec["stability_next_recover_at"] = None
    rec["stability_last_failure_at"] = None
    rec["stability_last_success_at"] = None


def _effective_process_version(cur_ver: str | None, rec: dict[str, Any], prev_proc: dict[str, Any]) -> str:
    return str(cur_ver or rec.get("current_version") or prev_proc.get("current_version") or "").strip()


def _sync_stability_version(rec: dict[str, Any], effective_version: str) -> None:
    failure_version = str(rec.get("stability_failure_version") or "").strip()
    if failure_version and effective_version and failure_version != effective_version:
        _clear_stability_backoff(rec)


def _record_pm2_probe_details(rec: dict[str, Any], probe_details: dict[str, Any] | None) -> None:
    if not isinstance(probe_details, dict):
        return
    mapping = {
        "pm_id": "pm_id",
        "pid": "pid",
        "pm2_status": "pm2_status",
        "pm2_restart_time": "pm2_restart_time",
        "pm2_uptime": "pm2_uptime",
        "runtime_seconds": "runtime_seconds",
        "memory": "memory",
        "cpu": "cpu",
    }
    for src, dst in mapping.items():
        if src in probe_details:
            rec[dst] = probe_details.get(src)
    if "pm2_restart_time" in probe_details:
        rec["stability_last_pm2_restart_time"] = probe_details.get("pm2_restart_time")


def _in_startup_grace(policy: dict[str, Any] | None, rec: dict[str, Any]) -> bool:
    if policy is None:
        return False
    runtime = rec.get("runtime_seconds")
    if runtime is None:
        return False
    try:
        return float(runtime) < _stability_int(policy, "startup_grace_seconds", 0)
    except (TypeError, ValueError):
        return False


def _note_stability_health_success(rec: dict[str, Any], policy: dict[str, Any] | None) -> None:
    if policy is None:
        return
    now = datetime.now(timezone.utc)
    stable_reset_seconds = _stability_int(policy, "stable_reset_seconds", 300)
    last_success = _parse_iso(str(rec.get("stability_last_success_at") or ""))
    if last_success is None:
        rec["stability_last_success_at"] = now.isoformat()
        return
    if last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=timezone.utc)
    if (now - last_success).total_seconds() >= stable_reset_seconds:
        _clear_stability_backoff(rec)
        rec["stability_last_success_at"] = now.isoformat()


def _note_stability_failure(
    rec: dict[str, Any],
    policy: dict[str, Any] | None,
    effective_version: str,
) -> None:
    if policy is None or not effective_version:
        return
    rec["stability_last_success_at"] = None
    rec["stability_failure_version"] = effective_version
    recovery_count = int(rec.get("stability_recovery_count", 0)) + 1
    rec["stability_recovery_count"] = recovery_count
    delay = _stability_backoff_seconds(policy, recovery_count)
    now = datetime.now(timezone.utc)
    rec["stability_last_failure_at"] = now.isoformat()
    rec["stability_next_recover_at"] = (now + timedelta(seconds=delay)).isoformat()


def _stability_recover_blocked(
    rec: dict[str, Any],
    policy: dict[str, Any] | None,
    effective_version: str,
    raw_status: str,
) -> bool:
    if policy is None or raw_status != "NOT_FOUND" or not effective_version:
        return False
    failure_version = str(rec.get("stability_failure_version") or "").strip()
    if failure_version != effective_version:
        return False
    next_recover_at = _parse_iso(str(rec.get("stability_next_recover_at") or ""))
    if next_recover_at is None:
        return False
    if next_recover_at.tzinfo is None:
        next_recover_at = next_recover_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < next_recover_at


def bootstrap_processes(
    cfg: dict[str, Any],
    cfg_path: str,
    state: dict[str, Any],
    prev_state: dict[str, Any],
    run_command_func,
) -> dict[str, subprocess.Popen[Any]]:
    """bootstrap 阶段进程处理：启动 + 温柔升级触发。"""
    scheduled_runners: dict[str, subprocess.Popen[Any]] = {}
    total = 0
    online = 0
    degraded = 0
    upgrading = 0
    failed = 0
    processes_payload: dict[str, Any] = {}
    effective_upgrade_states: dict[str, str] = {}
    for proc in cfg.get("processes", []) or []:
        if (
            isinstance(proc, dict)
            and str(proc.get("name", "")).strip()
            and process_is_applicable(proc, cfg)
        ):
            prev_proc = (prev_state.get("processes", {}) or {}).get(str(proc.get("name")), {})
            effective_upgrade_states[str(proc.get("name"))] = str((prev_proc or {}).get("upgrade_state", "stable")).strip() or "stable"

    for proc in cfg.get("processes", []) or []:
        name = proc["name"]
        prev_proc = (prev_state.get("processes", {}) or {}).get(name, {})
        if not process_is_applicable(proc, cfg):
            processes_payload[name] = _reconcile_inapplicable_process(proc, cfg, prev_proc)
            continue
        total += 1
        manager = proc.get("manager", "pm2")
        required = bool(proc.get("required", True))
        policy = _daemon_policy(proc, manager)
        bootstrap_skip_recover = bool(proc.get("bootstrap_skip_recover", False))
        rec = {
            "name": name,
            "manager": manager,
            "manager_type": manager,
            "manager_capability": "full",
            "required": required,
            "applicable": True,
            "daemon_policy": policy,
            "upgrade_state": "stable",
            "pending_target_version": None,
            "target_version": proc.get("target_version"),
            "current_version": None,
            "version_changed": False,
            "last_action": "none",
            "last_action_result": "skipped",
            "last_action_at": None,
            "health_failed_count": 0,
            "cooldown_until": None,
            "consecutive_failures": 0,
            "message": "",
            "last_check_time": now_iso(),
            "manager_raw_status": "",
            "manager_message": "",
            "status": "recovering",
            "meta_last_check_at": None,
            "last_update_time_ms": prev_proc.get("last_update_time_ms"),
            "last_update_result": prev_proc.get("last_update_result"),
            "last_update_target_version": prev_proc.get("last_update_target_version"),
        }
        try:
            strategy = get_strategy(manager)
        except ValueError as exc:
            rec["manager_capability"] = "unsupported"
            rec["status"] = "failed" if required else "disabled"
            rec["message"] = str(exc)
            processes_payload[name] = rec
            failed += 1 if required else 0
            continue

        cur_ver = _parse_version_probe(proc, run_command_func)
        if cur_ver:
            rec["current_version"] = cur_ver
        upgrade_cfg = proc.get("upgrade", {}) or {}
        if _is_upgrade_enabled(upgrade_cfg) and _bootstrap_should_schedule_upgrade(upgrade_cfg):
            if not _upgrade_dependencies_stable(upgrade_cfg, effective_upgrade_states):
                rec["upgrade_state"] = "pending"
                rec["pending_target_version"] = "auto"
                rec["status"] = "upgrading"
                rec["message"] = "upgrade pending until dependencies stable"
                rec["last_action"] = "upgrade"
                rec["last_action_result"] = "skipped"
                rec["last_action_at"] = now_iso()
                rec["meta_last_check_at"] = now_iso()
                effective_upgrade_states[name] = "pending"
                upgrading += 1
                processes_payload[name] = rec
                continue
            rec["upgrade_state"] = "upgrading"
            rec["pending_target_version"] = None
            rec["status"] = "upgrading"
            rec["message"] = "upgrade scheduled in bootstrap"
            rec["last_action"] = "upgrade"
            rec["last_action_result"] = "success"
            rec["last_action_at"] = now_iso()
            rec["meta_last_check_at"] = now_iso()
            if is_rootless_profile(cfg):
                runner = schedule_upgrade(
                    cfg_path,
                    name,
                    "auto",
                    independent_session=True,
                )
            else:
                runner = schedule_upgrade(cfg_path, name, "auto")
            scheduled_runners[name] = runner
            effective_upgrade_states[name] = "upgrading"
            upgrading += 1
            processes_payload[name] = rec
            continue

        if bootstrap_skip_recover:
            rec["status"] = "recovering" if required else "disabled"
            rec["message"] = "bootstrap recover skipped; daemon will reconcile"
            degraded += 1 if required else 0
            processes_payload[name] = rec
            continue

        probe = strategy.probe(proc, cfg)
        rec["manager_raw_status"] = probe.raw_status
        rec["manager_message"] = probe.message
        rec["status"] = _status_from_probe(probe.running, probe.transitional, required, recovering=False)
        rec["message"] = probe.message
        if str(probe.raw_status).strip() == "ERROR":
            rec["status"] = "recovering" if required else "disabled"

        need_action = False
        do_restart = False
        if policy == "ensure_exists":
            need_action = not probe.exists
        else:
            need_action = not probe.running and not probe.transitional
            do_restart = probe.exists
        if str(probe.raw_status).strip() == "ERROR":
            need_action = False

        if need_action:
            pre_res = _run_pre_recover_command(proc, run_command_func)
            if pre_res is not None and pre_res.returncode != 0:
                rec["last_action"] = "pre_recover"
                rec["last_action_result"] = "failed"
                rec["last_action_at"] = now_iso()
                rec["status"] = "failed" if required else "disabled"
                rec["message"] = pre_res.stderr.strip() or pre_res.stdout.strip() or "pre_recover_command failed"
                log(
                    "error",
                    "process.pre_recover.failed",
                    "进程恢复前置命令执行失败",
                    process=name,
                    manager=manager,
                    command=str(proc.get("pre_recover_command", "")),
                    error=rec["message"],
                )
                if rec["status"] == "failed":
                    failed += 1
                else:
                    degraded += 1
                processes_payload[name] = rec
                continue
            action_res = strategy.restart(proc, cfg) if do_restart else strategy.start(proc, cfg)
            rec["last_action"] = "restart" if do_restart else "start"
            rec["last_action_at"] = now_iso()
            if _is_action_ok(manager, action_res):
                rec["last_action_result"] = "success"
                rec["status"] = "recovering"
                rec["message"] = "bootstrap action applied"
            else:
                rec["last_action_result"] = "failed"
                rec["status"] = "failed" if required else "disabled"
                rec["message"] = action_res.stderr.strip() or action_res.stdout.strip() or rec["message"]

        if rec["status"] == "healthy":
            online += 1
        elif rec["status"] == "upgrading":
            upgrading += 1
        elif rec["status"] == "failed":
            failed += 1
        else:
            degraded += 1
        processes_payload[name] = rec

    state["processes"] = processes_payload
    state["summary"]["process_total"] = total
    state["summary"]["process_online"] = online
    state["summary"]["process_degraded"] = degraded
    state["summary"]["process_upgrading"] = upgrading
    state["summary"]["process_failed"] = failed
    return scheduled_runners


def reconcile_processes(
    cfg: dict[str, Any],
    cfg_path: str,
    state: dict[str, Any],
    prev_state: dict[str, Any],
    run_command_func,
) -> None:
    """执行进程巡检与恢复/升级调度。"""
    runtime = cfg["runtime"]
    retry_cfg = ((cfg.get("daemon", {}) or {}).get("upgrade_retry", {}) or {})
    base_delay_seconds = max(1, int(retry_cfg.get("base_delay_seconds", 5)))
    max_delay_seconds = max(base_delay_seconds, int(retry_cfg.get("max_delay_seconds", base_delay_seconds * 5)))
    max_retries = max(1, int(retry_cfg.get("max_retries", 3)))
    upgrade_event_missing_timeout_seconds = max(1, int((cfg.get("daemon", {}) or {}).get("upgrade_event_missing_timeout_seconds", 90)))
    events = load_upgrade_events(str(runtime["event_file"]))
    requests = load_upgrade_requests(str(runtime["upgrade_request_file"]))
    env_requests = load_env_requests(cfg)
    xagent_env_request_ids = _xagent_env_request_ids(env_requests)
    event_map: dict[str, dict[str, Any]] = {}
    for event in events:
        if isinstance(event, dict) and event.get("process"):
            event_map[event["process"]] = event
    request_map: dict[str, dict[str, Any]] = {}
    for req in requests:
        if isinstance(req, dict) and req.get("process"):
            request_map[str(req["process"])] = req

    total = 0
    online = 0
    degraded = 0
    upgrading = 0
    failed = 0
    processes_payload: dict[str, Any] = {}
    applicable_processes = [
        proc
        for proc in cfg.get("processes", []) or []
        if isinstance(proc, dict) and process_is_applicable(proc, cfg)
    ]
    effective_upgrade_states = _effective_upgrade_states(
        applicable_processes,
        (prev_state.get("processes", {}) or {}),
        event_map,
    )
    any_upgrading = any(state in {"upgrading", "pending"} for state in effective_upgrade_states.values())

    for proc in cfg.get("processes", []) or []:
        name = proc["name"]
        prev_proc = (prev_state.get("processes", {}) or {}).get(name, {})
        if not process_is_applicable(proc, cfg):
            processes_payload[name] = _reconcile_inapplicable_process(proc, cfg, prev_proc)
            continue
        total += 1
        manager = proc.get("manager", "pm2")
        policy = _daemon_policy(proc, manager)
        required = bool(proc.get("required", True))
        cooldown_sec = int(proc.get("recover_cooldown_seconds", 15))
        cooldown_until = prev_proc.get("cooldown_until")
        in_cooldown = False
        if cooldown_until:
            try:
                in_cooldown = datetime.fromisoformat(str(cooldown_until)) > datetime.now(timezone.utc)
            except Exception:
                in_cooldown = False
        rec = {
            "name": name,
            "manager": manager,
            "manager_type": manager,
            "manager_capability": "full",
            "required": required,
            "applicable": True,
            "upgrade_state": prev_proc.get("upgrade_state", "stable"),
            "pending_target_version": prev_proc.get("pending_target_version"),
            "health_failed_count": int(prev_proc.get("health_failed_count", 0)),
            "upgrade_retry_count": int(prev_proc.get("upgrade_retry_count", 0)),
            "upgrade_next_retry_at": prev_proc.get("upgrade_next_retry_at"),
            "upgrade_last_error": prev_proc.get("upgrade_last_error"),
            "upgrade_pending_reason": prev_proc.get("upgrade_pending_reason"),
            "upgrade_idle_started_at": prev_proc.get("upgrade_idle_started_at"),
            "upgrade_force_after": prev_proc.get("upgrade_force_after"),
            "upgrade_idle_last_probe_at": prev_proc.get("upgrade_idle_last_probe_at"),
            "upgrade_idle_last_status": prev_proc.get("upgrade_idle_last_status"),
            "upgrade_idle_last_error": prev_proc.get("upgrade_idle_last_error"),
            "target_version": proc.get("target_version"),
            "current_version": prev_proc.get("current_version"),
            "version_changed": False,
            "last_action": "none",
            "last_action_result": "skipped",
            "last_action_at": prev_proc.get("last_action_at"),
            "cooldown_until": cooldown_until,
            "consecutive_failures": int(prev_proc.get("consecutive_failures", 0)),
            "message": "",
            "daemon_policy": policy,
            "last_check_time": now_iso(),
            "manager_raw_status": "",
            "manager_message": "",
            "status": "recovering",
            "meta_last_check_at": prev_proc.get("meta_last_check_at"),
            "stability_failure_version": prev_proc.get("stability_failure_version"),
            "stability_recovery_count": int(prev_proc.get("stability_recovery_count", 0)),
            "stability_next_recover_at": prev_proc.get("stability_next_recover_at"),
            "stability_last_failure_at": prev_proc.get("stability_last_failure_at"),
            "stability_last_success_at": prev_proc.get("stability_last_success_at"),
            "stability_last_pm2_restart_time": prev_proc.get("stability_last_pm2_restart_time"),
            "last_update_time_ms": prev_proc.get("last_update_time_ms"),
            "last_update_result": prev_proc.get("last_update_result"),
            "last_update_target_version": prev_proc.get("last_update_target_version"),
        }
        if (
            str(prev_proc.get("upgrade_state", "")).strip() == "upgrade_failed"
            and str(prev_proc.get("upgrade_retry_target_version", "")).strip()
        ):
            rec["upgrade_retry_target_version"] = str(prev_proc["upgrade_retry_target_version"]).strip()

        ev = event_map.get(name)
        if ev:
            rec["upgrade_task_id"] = ev.get("task_id")
            rec["upgrade_finished_at"] = ev.get("finished_at")
            rec["last_action"] = "upgrade"
            event_skipped = bool(ev.get("skipped"))
            if not event_skipped:
                rec["last_update_time_ms"] = _iso_to_ms(ev.get("finished_at")) or _now_ms()
                rec["last_update_target_version"] = str(ev.get("target_version") or "")
            if ev.get("ok"):
                rec["upgrade_state"] = "stable"
                rec.pop("upgrade_retry_target_version", None)
                rec["pending_target_version"] = None
                rec["upgrade_retry_count"] = 0
                rec["upgrade_next_retry_at"] = None
                rec["upgrade_last_error"] = None
                _clear_upgrade_idle_fields(rec)
                rec["current_version"] = ev.get("target_version") or rec["current_version"]
                rec["last_action_result"] = str(ev.get("post_action_result") or "success")
                if not event_skipped:
                    rec["last_update_result"] = 1
                if ev.get("skipped"):
                    rec["message"] = "already latest"
                if ev.get("post_action"):
                    rec["post_upgrade_action"] = ev.get("post_action")
            elif ev.get("skipped"):
                rec["upgrade_state"] = "stable"
                rec.pop("upgrade_retry_target_version", None)
                rec["pending_target_version"] = None
                rec["upgrade_retry_count"] = 0
                rec["upgrade_next_retry_at"] = None
                rec["upgrade_last_error"] = None
                _clear_upgrade_idle_fields(rec)
                rec["current_version"] = ev.get("target_version") or rec["current_version"]
                rec["last_action_result"] = str(ev.get("post_action_result") or "failed")
                rec["message"] = "already latest; daemon will reconcile start"
                log(
                    "warn",
                    "daemon.upgrade.skip_latest_start_failed",
                    "已是最新版本，但 upgrade-runner 启动失败，回退到 daemon 直接巡检",
                    process=name,
                    task_id=ev.get("task_id"),
                    error=str(ev.get("error", "")),
                )
            else:
                rec["upgrade_state"] = "upgrade_failed"
                rec["upgrade_error"] = ev.get("error")
                rec["upgrade_last_error"] = ev.get("error")
                _clear_upgrade_idle_fields(rec)
                rec["last_action_result"] = "failed"
                rec["last_update_result"] = 0
                rec["upgrade_retry_count"] = int(prev_proc.get("upgrade_retry_count", 0)) + 1
                if _is_disk_insufficient_upgrade_error(ev.get("error")):
                    rec.pop("upgrade_retry_target_version", None)
                    rec["upgrade_next_retry_at"] = None
                    rec["message"] = "upgrade failed; retry skipped because disk is insufficient"
                elif rec["upgrade_retry_count"] < max_retries:
                    rec["upgrade_retry_target_version"] = (
                        str(ev.get("requested_target_version") or "auto").strip() or "auto"
                    )
                    delay = _retry_delay_seconds(rec["upgrade_retry_count"], base_delay_seconds, max_delay_seconds)
                    rec["upgrade_next_retry_at"] = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                    rec["message"] = f"upgrade failed; retry scheduled in {delay}s"
                else:
                    rec.pop("upgrade_retry_target_version", None)
                    rec["upgrade_next_retry_at"] = None
                    rec["message"] = "upgrade failed; retry exhausted"
                log(
                    "error",
                    "daemon.upgrade.failed",
                    "检测到升级失败事件",
                    process=name,
                    task_id=ev.get("task_id"),
                    error=str(ev.get("error", "")),
                    retry_count=rec["upgrade_retry_count"],
                    max_retries=max_retries,
                )

        try:
            strategy = get_strategy(manager)
        except ValueError as exc:
            rec["manager_capability"] = "unsupported"
            rec["status"] = "failed" if required else "disabled"
            rec["message"] = str(exc)
            processes_payload[name] = rec
            failed += 1 if required else 0
            continue

        cur_ver = _parse_version_probe(proc, run_command_func)
        if cur_ver:
            rec["current_version"] = cur_ver
        effective_version = _effective_process_version(cur_ver, rec, prev_proc)
        upgrade_cfg = proc.get("upgrade", {}) or {}
        process_upgrade_event_timeout_seconds = max(
            1,
            int(upgrade_cfg.get("event_missing_timeout_seconds", upgrade_event_missing_timeout_seconds)),
        )
        stability_cfg = _stability_policy(proc, manager)
        _sync_stability_version(rec, effective_version)
        force_req = request_map.get(name)
        idle_gate_deferred = False
        if force_req and _is_upgrade_enabled(upgrade_cfg):
            current_upgrade_state = str(rec.get("upgrade_state", "stable")).strip()
            if current_upgrade_state == "upgrading" and _is_stale_upgrading(
                prev_proc,
                timeout_seconds=process_upgrade_event_timeout_seconds,
            ):
                force_req = None
            elif current_upgrade_state in {"pending", "upgrading"}:
                rec["status"] = "upgrading"
                rec["message"] = f"force upgrade ignored because upgrade is already {current_upgrade_state}"
                rec["last_action"] = "upgrade"
                rec["last_action_result"] = "skipped"
                rec["last_action_at"] = now_iso()
                effective_upgrade_states[name] = current_upgrade_state
                any_upgrading = True
                upgrading += 1
                processes_payload[name] = rec
                continue
        if force_req and _is_upgrade_enabled(upgrade_cfg):
            forced_target_version = str(force_req.get("target_version") or "auto").strip() or "auto"
            rec.pop("upgrade_retry_target_version", None)
            if not _upgrade_dependencies_stable(upgrade_cfg, effective_upgrade_states):
                rec["upgrade_state"] = "pending"
                rec["pending_target_version"] = forced_target_version
                rec["upgrade_pending_reason"] = "dependencies"
                rec["status"] = "upgrading"
                rec["message"] = f"force upgrade pending until dependencies stable: {force_req.get('reason', 'external_trigger')}"
                rec["last_action"] = "upgrade"
                rec["last_action_result"] = "skipped"
                rec["last_action_at"] = now_iso()
                rec["meta_last_check_at"] = now_iso()
                effective_upgrade_states[name] = "pending"
                any_upgrading = True
                upgrading += 1
                processes_payload[name] = rec
                continue
            if _should_defer_xagent_upgrade_for_idle(proc, rec, prev_proc, forced_target_version):
                idle_gate_deferred = True
            else:
                _clear_upgrade_idle_fields(rec)
                rec["upgrade_state"] = "upgrading"
                rec["pending_target_version"] = None
                rec["status"] = "upgrading"
                rec["message"] = f"force upgrade requested: {force_req.get('reason', 'external_trigger')}"
                rec["last_action"] = "upgrade"
                rec["last_action_result"] = "success"
                rec["last_action_at"] = now_iso()
                rec["meta_last_check_at"] = now_iso()
                schedule_upgrade(cfg_path, name, forced_target_version)
                effective_upgrade_states[name] = "upgrading"
                any_upgrading = True
                upgrading += 1
                processes_payload[name] = rec
                continue

        became_applicable = prev_proc.get("applicable") is False
        if (
            became_applicable
            and _is_upgrade_enabled(upgrade_cfg)
            and _bootstrap_should_schedule_upgrade(upgrade_cfg)
        ):
            rec["last_action"] = "upgrade"
            rec["last_action_at"] = now_iso()
            rec["meta_last_check_at"] = now_iso()
            if not _upgrade_dependencies_stable(upgrade_cfg, effective_upgrade_states):
                rec["upgrade_state"] = "pending"
                rec["pending_target_version"] = "auto"
                rec["upgrade_pending_reason"] = "dependencies"
                rec["status"] = "upgrading"
                rec["message"] = "newly applicable process upgrade pending until dependencies stable"
                rec["last_action_result"] = "skipped"
                effective_upgrade_states[name] = "pending"
            else:
                schedule_upgrade(cfg_path, name, "auto")
                rec["upgrade_state"] = "upgrading"
                rec["pending_target_version"] = None
                rec["upgrade_pending_reason"] = "applicability_transition"
                rec["status"] = "upgrading"
                rec["message"] = "process became applicable; upgrade scheduled"
                rec["last_action_result"] = "success"
                effective_upgrade_states[name] = "upgrading"
            any_upgrading = True
            upgrading += 1
            processes_payload[name] = rec
            continue

        if (
            not idle_gate_deferred
            and rec["upgrade_state"] == "pending"
            and _is_upgrade_enabled(upgrade_cfg)
        ):
            pending_target = str(rec.get("pending_target_version") or "auto").strip() or "auto"
            if not _upgrade_dependencies_stable(upgrade_cfg, effective_upgrade_states):
                rec["status"] = "upgrading"
                rec["message"] = "upgrade pending until dependencies stable"
                rec["upgrade_pending_reason"] = rec.get("upgrade_pending_reason") or "dependencies"
                effective_upgrade_states[name] = "pending"
                any_upgrading = True
                upgrading += 1
                processes_payload[name] = rec
                continue
            if _is_xagent_idle_pending(rec) and _should_defer_xagent_upgrade_for_idle(proc, rec, prev_proc, pending_target):
                idle_gate_deferred = True
            else:
                _clear_upgrade_idle_fields(rec)
                schedule_upgrade(cfg_path, name, pending_target)
                rec["upgrade_state"] = "upgrading"
                rec["pending_target_version"] = None
                rec["status"] = "upgrading"
                rec["last_action"] = "upgrade"
                rec["last_action_result"] = "success"
                rec["last_action_at"] = now_iso()
                rec["meta_last_check_at"] = now_iso()
                rec["message"] = "pending upgrade scheduled after dependencies stable"
                effective_upgrade_states[name] = "upgrading"
                any_upgrading = True
                upgrading += 1
                processes_payload[name] = rec
                continue

        if rec["upgrade_state"] == "upgrade_failed" and _is_upgrade_enabled(upgrade_cfg):
            retry_count = int(rec.get("upgrade_retry_count", 0))
            retry_target = str(rec.get("upgrade_retry_target_version") or "auto").strip() or "auto"
            next_retry_at = _parse_iso(str(rec.get("upgrade_next_retry_at") or ""))
            due = next_retry_at is not None and datetime.now(timezone.utc) >= next_retry_at
            if retry_count < max_retries and due:
                if not _upgrade_dependencies_stable(upgrade_cfg, effective_upgrade_states):
                    rec["upgrade_state"] = "pending"
                    rec["pending_target_version"] = retry_target
                    rec.pop("upgrade_retry_target_version", None)
                    rec["status"] = "upgrading"
                    rec["message"] = "retry pending until dependencies stable"
                    effective_upgrade_states[name] = "pending"
                    any_upgrading = True
                    upgrading += 1
                    processes_payload[name] = rec
                    continue
                schedule_upgrade(cfg_path, name, retry_target)
                rec["upgrade_state"] = "upgrading"
                rec.pop("upgrade_retry_target_version", None)
                rec["pending_target_version"] = None
                rec["status"] = "upgrading"
                rec["last_action"] = "upgrade"
                rec["last_action_result"] = "success"
                rec["last_action_at"] = now_iso()
                rec["meta_last_check_at"] = now_iso()
                rec["message"] = "scheduled retry after previous upgrade failure"
                effective_upgrade_states[name] = "upgrading"
                any_upgrading = True
                upgrading += 1
                processes_payload[name] = rec
                continue
            if retry_count >= max_retries or next_retry_at is None:
                rec.pop("upgrade_retry_target_version", None)

        if rec["upgrade_state"] == "upgrading":
            if _is_stale_upgrading(prev_proc, timeout_seconds=process_upgrade_event_timeout_seconds):
                rec["upgrade_state"] = "stable"
                rec["message"] = "upgrade event missing; fallback to direct reconcile"
                log(
                    "warn",
                    "daemon.upgrade.event_missing",
                    "长时间未收到 upgrade 结果事件，回退到直接巡检",
                    process=name,
                    timeout_seconds=process_upgrade_event_timeout_seconds,
                )
            else:
                rec["status"] = "upgrading"
                rec["message"] = "upgrade in progress"
                upgrading += 1
                processes_payload[name] = rec
                continue

        if (
            manager == "pm2"
            and _defer_recovery_for_active_upgrades(
                proc,
                effective_upgrade_states,
                any_upgrading=any_upgrading,
            )
        ):
            prev_status = str(prev_proc.get("status", "")).strip()
            rec["status"] = prev_status if prev_status in {"healthy", "recovering", "disabled"} else ("recovering" if required else "disabled")
            rec["message"] = "probe and recovery deferred while another upgrade is in progress"
            degraded += 1 if rec["status"] != "healthy" else 0
            online += 1 if rec["status"] == "healthy" else 0
            processes_payload[name] = rec
            continue

        probe = strategy.probe(proc, cfg)
        rec["manager_raw_status"] = probe.raw_status
        rec["manager_message"] = probe.message
        _record_pm2_probe_details(rec, probe.details)

        if manager == "pm2" and str(probe.raw_status).strip() == "DUPLICATE":
            instances = list(((probe.details or {}).get("instances") or []))
            rec["duplicate_instances"] = instances
            pm_ids = [item.get("pm_id") for item in instances if isinstance(item, dict)]
            if hasattr(strategy, "delete_instances"):
                action_res = strategy.delete_instances(proc, cfg, pm_ids)
            else:
                action_res = CommandResult(returncode=2, stdout="", stderr="pm2 strategy does not support precise duplicate deletion")
            rec["last_action"] = "delete_duplicates"
            rec["last_action_at"] = now_iso()
            rec["status"] = "recovering" if required else "disabled"
            if _is_action_ok(manager, action_res):
                rec["last_action_result"] = "success"
                rec["message"] = "duplicate pm2 instances deleted; waiting next-cycle reconcile"
                log(
                    "warn",
                    "daemon.pm2.delete_duplicates",
                    f"{name} 检测到重复 PM2 实例，已按 pm_id 删除，等待下一轮巡检",
                    process=name,
                    pm_ids=pm_ids,
                    instances=instances,
                )
            else:
                rec["last_action_result"] = "failed"
                rec["message"] = action_res.stderr.strip() or action_res.stdout.strip() or probe.message
                log(
                    "warn",
                    "daemon.pm2.delete_duplicates_failed",
                    f"{name} 检测到重复 PM2 实例，但删除失败",
                    process=name,
                    pm_ids=pm_ids,
                    instances=instances,
                    returncode=action_res.returncode,
                    error=rec["message"],
                )
            processes_payload[name] = rec
            degraded += 1 if rec["status"] != "healthy" else 0
            continue

        if manager == "pm2" and _is_xagent_package(proc) and xagent_env_request_ids:
            raw_status = str(probe.raw_status).strip()
            if raw_status == "ERROR" or probe.transitional:
                log(
                    "warn",
                    "daemon.env_request.defer",
                    "xagent env request 暂缓处理，PM2 状态不确定",
                    process=name,
                    raw_status=raw_status,
                    message=probe.message,
                )
            elif not probe.exists:
                remove_consumed_env_requests(cfg, xagent_env_request_ids)
                log(
                    "info",
                    "daemon.env_request.consumed",
                    "xagent 不存在，env request 已消费，后续启动会读取新环境变量",
                    process=name,
                    request_count=len(xagent_env_request_ids),
                )
            elif str(rec.get("upgrade_state", "stable")).strip() == "stable":
                action_res = strategy.delete(proc, cfg)
                rec["last_action"] = "delete"
                rec["last_action_at"] = now_iso()
                rec["health_failed_count"] = 0
                if _is_action_ok(manager, action_res) or _is_pm2_delete_not_found(action_res):
                    remove_consumed_env_requests(cfg, xagent_env_request_ids)
                    rec["stability_next_recover_at"] = None
                    rec["stability_last_success_at"] = None
                    rec["last_action_result"] = "success"
                    rec["status"] = "recovering"
                    rec["message"] = "xagent env changed; deleted and waiting next-cycle start"
                    log(
                        "info",
                        "daemon.env_request.xagent_deleted",
                        "xagent env 变化，已 delete 当前 PM2 进程，等待下一轮按新环境启动",
                        process=name,
                        request_count=len(xagent_env_request_ids),
                    )
                else:
                    rec["last_action_result"] = "failed"
                    rec["status"] = "recovering" if required else "disabled"
                    rec["message"] = action_res.stderr.strip() or action_res.stdout.strip() or probe.message
                    log(
                        "warn",
                        "daemon.env_request.delete_failed",
                        "xagent env request 处理时 delete 失败，保留 request",
                        process=name,
                        error=rec["message"],
                    )
                processes_payload[name] = rec
                degraded += 1 if rec["status"] != "healthy" else 0
                continue

        rec["status"] = _status_from_probe(probe.running, probe.transitional, required, in_cooldown)
        if not rec.get("message"):
            rec["message"] = probe.message
        if manager == "pm2" and str(probe.raw_status).strip() == "ERROR":
            rec["status"] = "recovering" if required else "disabled"
        if manager == "supervisor" and str(probe.raw_status).strip() == "ERROR":
            rec["status"] = "recovering" if required else "disabled"

        if manager == "pm2" and str(probe.raw_status).strip().lower() == "errored" and hasattr(strategy, "delete"):
            _note_stability_failure(rec, stability_cfg, effective_version)
            action_res = strategy.delete(proc, cfg)
            rec["last_action"] = "delete"
            rec["last_action_at"] = now_iso()
            rec["health_failed_count"] = 0
            if _is_action_ok(manager, action_res):
                rec["last_action_result"] = "success"
                rec["status"] = "recovering"
                rec["message"] = "pm2 errored; deleted and waiting next-cycle start"
                log_data = {"process": name}
                if stability_cfg is not None:
                    log_data.update(
                        {
                            "stability_failure_version": rec.get("stability_failure_version"),
                            "stability_failure_reason": "pm2_errored",
                            "stability_recovery_count": rec.get("stability_recovery_count"),
                            "stability_next_recover_at": rec.get("stability_next_recover_at"),
                        }
                    )
                log(
                    "warn",
                    "daemon.pm2.delete_after_errored",
                    f"{name} 已进入 pm2 errored，已执行 delete，等待下一轮拉起",
                    **log_data,
                )
            else:
                rec["last_action_result"] = "failed"
                rec["status"] = "recovering" if required else "disabled"
                rec["message"] = action_res.stderr.strip() or action_res.stdout.strip() or rec["message"]
            processes_payload[name] = rec
            degraded += 1 if rec["status"] != "healthy" else 0
            continue

        if probe.running:
            health_ok, health_msg = _run_health_check(proc, run_command_func)
            if health_ok:
                rec["health_failed_count"] = 0
                _note_stability_health_success(rec, stability_cfg)
                if health_msg:
                    rec["message"] = health_msg
            elif _in_startup_grace(stability_cfg, rec):
                rec["status"] = "recovering" if required else "disabled"
                rec["message"] = f"startup grace: {health_msg}"
                rec["health_failed_count"] = int(prev_proc.get("health_failed_count", 0))
            else:
                if stability_cfg is not None:
                    rec["stability_last_success_at"] = None
                rec["status"] = "failed" if required else "disabled"
                rec["message"] = health_msg
                threshold = _health_failure_threshold(proc)
                if threshold > 0:
                    rec["health_failed_count"] = int(prev_proc.get("health_failed_count", 0)) + 1
                    if rec["health_failed_count"] >= threshold and manager == "pm2" and hasattr(strategy, "delete"):
                        _note_stability_failure(rec, stability_cfg, effective_version)
                        action_res = strategy.delete(proc, cfg)
                        rec["last_action"] = "delete"
                        rec["last_action_at"] = now_iso()
                        if _is_action_ok(manager, action_res):
                            rec["last_action_result"] = "success"
                            rec["status"] = "recovering"
                            rec["health_failed_count"] = 0
                            rec["message"] = "health failure threshold reached; deleted and waiting next-cycle start"
                            log_data = {"process": name, "threshold": threshold}
                            if stability_cfg is not None:
                                log_data.update(
                                    {
                                        "stability_failure_version": rec.get("stability_failure_version"),
                                        "stability_failure_reason": "health_check",
                                        "stability_recovery_count": rec.get("stability_recovery_count"),
                                        "stability_next_recover_at": rec.get("stability_next_recover_at"),
                                    }
                                )
                            log(
                                "warn",
                                "daemon.health.delete_after_failures",
                                f"{name} 连续 {threshold} 次健康探针失败，已执行 delete，等待下一轮拉起",
                                **log_data,
                            )
                        else:
                            rec["last_action_result"] = "failed"
                            rec["status"] = "recovering" if required else "disabled"
                            rec["message"] = action_res.stderr.strip() or action_res.stdout.strip() or rec["message"]
                        processes_payload[name] = rec
                        degraded += 1 if rec["status"] != "healthy" else 0
                        continue
                    if manager == "direct" and rec["health_failed_count"] < threshold:
                        rec["status"] = "recovering" if required else "disabled"
                        rec["message"] = (
                            f"health check failed {rec['health_failed_count']}/{threshold}; "
                            "waiting for restart threshold"
                        )

        if policy == "ensure_exists" and probe.exists and not probe.running:
            # PM2 自愈场景：进程已被 PM2 托管但暂时不在线，标记 recovering，不主动频繁重启。
            rec["status"] = "recovering"
            rec["message"] = "managed by pm2, waiting autorestart"

        should_recover = rec["status"] == "failed"
        if policy == "ensure_exists":
            should_recover = (not probe.exists) and str(probe.raw_status).strip() == "NOT_FOUND"
        if manager == "supervisor" and str(probe.raw_status).strip() == "ERROR":
            should_recover = False

        if should_recover and _defer_recovery_for_active_upgrades(
            proc,
            effective_upgrade_states,
            any_upgrading=any_upgrading,
        ):
            rec["status"] = "recovering" if required else "disabled"
            rec["message"] = "recovery deferred while another upgrade is in progress"
            degraded += 1 if required else 0
            processes_payload[name] = rec
            continue

        if should_recover and _stability_recover_blocked(rec, stability_cfg, effective_version, str(probe.raw_status).strip()):
            rec["status"] = "recovering" if required else "disabled"
            rec["message"] = f"health recovery backoff until {rec.get('stability_next_recover_at')}"
            degraded += 1 if required else 0
            processes_payload[name] = rec
            continue

        if should_recover and not in_cooldown and rec["upgrade_state"] != "upgrading":
            do_restart = probe.exists and policy == "ensure_running"
            pre_res = _run_pre_recover_command(proc, run_command_func)
            if pre_res is not None and pre_res.returncode != 0:
                rec["last_action"] = "pre_recover"
                rec["last_action_result"] = "failed"
                rec["last_action_at"] = now_iso()
                rec["consecutive_failures"] += 1
                rec["cooldown_until"] = datetime.fromtimestamp(time.time() + cooldown_sec, timezone.utc).isoformat()
                rec["message"] = pre_res.stderr.strip() or pre_res.stdout.strip() or "pre_recover_command failed"
                rec["status"] = "failed" if required else "disabled"
                log(
                    "error",
                    "process.pre_recover.failed",
                    "进程恢复前置命令执行失败",
                    process=name,
                    manager=manager,
                    command=str(proc.get("pre_recover_command", "")),
                    error=rec["message"],
                )
                if rec["status"] == "failed":
                    failed += 1
                else:
                    degraded += 1
                processes_payload[name] = rec
                continue
            if do_restart:
                action_res = strategy.restart(proc, cfg)
            elif _is_xagent_package(proc) and hasattr(strategy, "start_xagent"):
                action_res = strategy.start_xagent(proc, cfg, cleanup_before_start=False)
            else:
                action_res = strategy.start(proc, cfg)
            rec["last_action"] = "restart" if do_restart else "start"
            rec["last_action_at"] = now_iso()
            if _is_action_ok(manager, action_res):
                rec["last_action_result"] = "success"
                rec["status"] = "recovering"
                rec["cooldown_until"] = datetime.fromtimestamp(time.time() + cooldown_sec, timezone.utc).isoformat()
                rec["consecutive_failures"] = 0
                rec["health_failed_count"] = 0
            else:
                rec["last_action_result"] = "failed"
                rec["consecutive_failures"] += 1
                rec["cooldown_until"] = datetime.fromtimestamp(time.time() + cooldown_sec, timezone.utc).isoformat()
                rec["message"] = action_res.stderr.strip() or action_res.stdout.strip() or rec["message"]

        if rec["status"] == "healthy":
            online += 1
        elif rec["status"] == "upgrading":
            upgrading += 1
        elif rec["status"] == "failed":
            failed += 1
        else:
            degraded += 1
        processes_payload[name] = rec

    state["processes"] = processes_payload
    state["summary"]["process_total"] = total
    state["summary"]["process_online"] = online
    state["summary"]["process_degraded"] = degraded
    state["summary"]["process_upgrading"] = upgrading
    state["summary"]["process_failed"] = failed
