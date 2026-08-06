from __future__ import annotations

"""daemon 主循环执行逻辑。"""

import time
import uuid
from typing import Any

from pathlib import Path
from datetime import datetime, timezone

from .common import FileLock, load_json, log, now_iso, run_command, write_json_atomic
from .config import apply_defaults, read_config, validate_config
from .env_store import restore_env_cache_to_process
from .pm2_log_cleanup import reconcile_pm2_log_cleanup
from .presentation import daemon_cycle_summary
from .reconcile.env import reconcile_env
from .reconcile.processes import reconcile_processes
from .report import reconcile_report
from .resources import collect_resource_usage
from .skills import reconcile_skill_sync
from .state import build_base_state, finalize_overall_status
from .workdir_cleanup import reconcile_workdir_cleanup
from .xagent_activity_notify import reconcile_xagent_activity_notify
from .xagent_heartbeat_control import reconcile_xagent_heartbeat_control


def _is_stale_bootstrapping(prev: dict[str, Any], timeout_seconds: int = 60) -> bool:
    """判断 bootstrapping 状态是否已陈旧。"""
    if prev.get("phase") != "bootstrapping":
        return False
    ts = str(prev.get("last_cycle_time") or "").strip()
    if not ts:
        return True
    try:
        last = datetime.fromisoformat(ts)
    except Exception:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() > timeout_seconds


def run_daemon_once(cfg: dict[str, Any], cfg_path: str) -> int:
    """执行单轮巡检。"""
    runtime = cfg["runtime"]
    state_file = runtime["state_file"]
    prev = load_json(state_file)
    if prev.get("phase") == "bootstrapping":
        if not _is_stale_bootstrapping(prev, timeout_seconds=60):
            return 0
        log("warn", "daemon.bootstrap.stale", "检测到陈旧的 bootstrapping 状态，daemon 接管继续巡检")

    state = build_base_state(cfg, phase="running", prev=prev)
    state["bootstrap_epoch"] = prev.get("bootstrap_epoch")
    exit_code = 0

    try:
        try:
            state["resources"] = collect_resource_usage(prev.get("resources", {}) or {})
        except Exception as exc:
            state["resources"] = dict((prev.get("resources", {}) or {}))
            state["errors"].append(f"resources: {exc}")
            exit_code = 1

        try:
            reconcile_env(cfg, state, phase="daemon")
        except Exception as exc:
            state["env"] = {"enabled": True, "status": "failed", "message": f"env reconcile error: {exc}"}
            state["errors"].append(f"env: {exc}")
            exit_code = 1

        try:
            reconcile_processes(cfg, cfg_path, state, prev, run_command)
        except Exception as exc:
            state["summary"]["process_failed"] = max(1, int(state["summary"].get("process_failed", 0)))
            state["errors"].append(f"processes: {exc}")
            exit_code = 1

        # Best-effort dependency control: process reconciliation remains authoritative.
        try:
            reconcile_xagent_heartbeat_control(cfg, state, prev)
        except Exception as exc:
            previous_control = prev.get("xagent_heartbeat_control", {})
            control_state = dict(previous_control) if isinstance(previous_control, dict) else {}
            control_state["enabled"] = bool(
                (cfg.get("xagent_heartbeat_control", {}) or {}).get("enabled", False)
            )
            control_state["last_error"] = str(exc)
            state["xagent_heartbeat_control"] = control_state
            log(
                "warn",
                "daemon.xagent_heartbeat.reconcile_failed",
                "xagent 心跳控制模块执行失败",
                error=str(exc),
            )

        try:
            reconcile_workdir_cleanup(cfg, state, prev)
        except Exception as exc:
            state["workdir_cleanup"] = {"enabled": True, "status": "failed", "message": str(exc)}
            state["errors"].append(f"workdir_cleanup: {exc}")
            exit_code = 1

        # PM2 daemon 日志清理为 best-effort，不调用 PM2 命令，也不影响进程巡检。
        try:
            reconcile_pm2_log_cleanup(cfg, state, prev)
        except Exception as exc:
            state["pm2_log_cleanup"] = {"enabled": True, "status": "failed", "message": str(exc)}
            log("warn", "daemon.pm2_log.cleanup_failed", "PM2 daemon 日志清理异常", error=str(exc))

        try:
            reconcile_skill_sync(cfg, cfg_path, state, prev)
        except Exception as exc:
            state["errors"].append(f"skills: {exc}")
            exit_code = 1

        # Last normal scan task. It is best-effort and must not affect daemon health.
        try:
            reconcile_xagent_activity_notify(cfg, state, prev)
        except Exception as exc:
            previous_activity = prev.get("xagent_activity_notify", {})
            activity_state = dict(previous_activity) if isinstance(previous_activity, dict) else {}
            activity_state["enabled"] = bool(
                (cfg.get("xagent_activity_notify", {}) or {}).get("enabled", False)
            )
            activity_state["last_error"] = str(exc)
            state["xagent_activity_notify"] = activity_state
            log(
                "warn",
                "daemon.xagent_activity.reconcile_failed",
                "xagent 会话活动探测模块执行失败",
                error=str(exc),
            )
    except Exception as exc:
        # 兜底：任何未预期异常都要记录并继续落状态，避免状态文件长期不更新。
        state["errors"].append(f"daemon_unhandled: {exc}")
        exit_code = 1
    finally:
        finalize_overall_status(state)
        try:
            reconcile_report(cfg, state, prev)
        except Exception as exc:
            state["report"] = {
                "enabled": bool((cfg.get("report", {}) or {}).get("enabled", False)),
                "status": "degraded",
                "last_error": str(exc),
            }
            log("warn", "report.reconcile_failed", "运行态上报模块执行失败", error=str(exc))
        write_json_atomic(state_file, state)
        level, msg = daemon_cycle_summary(state)
        log(level, "daemon.cycle.summary", msg)

    return exit_code


def _load_config_or_raise(cfg_path: str, app_version: str) -> dict[str, Any]:
    """读取并校验配置。"""
    cfg = apply_defaults(read_config(cfg_path), app_version)
    errors = validate_config(cfg)
    if errors:
        raise ValueError("配置校验失败:\n- " + "\n- ".join(errors))
    return cfg


def _guess_state_file(cfg_path: str) -> str | None:
    """推断 state 文件路径；配置不可用时返回 None。"""
    try:
        raw = read_config(cfg_path)
        if not isinstance(raw, dict):
            return None
        runtime = raw.get("runtime", {}) or {}
        return str(runtime["state_file"])
    except Exception:
        return None


def _write_error_state(cfg_path: str, app_version: str, message: str) -> None:
    """在配置不可用场景下也写出失败状态，避免状态长期停滞。"""
    state_file = _guess_state_file(cfg_path)
    if not state_file:
        return
    prev = load_json(state_file)
    state: dict[str, Any] = {
        "daemon_version": app_version,
        "phase": str(prev.get("phase", "running")),
        "bootstrap_epoch": prev.get("bootstrap_epoch"),
        "overall_status": "failed",
        "sandbox_status": "failed",
        "last_cycle_id": str(uuid.uuid4()),
        "last_cycle_time": now_iso(),
        "summary": prev.get(
            "summary",
            {
                "process_total": 0,
                "process_online": 0,
                "process_degraded": 0,
                "process_upgrading": 0,
                "process_failed": 0,
            },
        ),
        "env": prev.get("env", {"enabled": False, "status": "ok"}),
        "processes": prev.get("processes", {}),
        "errors": [message],
    }
    write_json_atomic(state_file, state)


def _daemon_cycle(
    cfg_path: str,
    app_version: str,
) -> int:
    """执行 daemon 单轮：读取本地配置 -> 巡检。"""
    try:
        cfg = _load_config_or_raise(cfg_path, app_version)
    except Exception as exc:
        err = f"config_invalid: 配置读取或校验失败，本轮跳过巡检: {exc}"
        _write_error_state(cfg_path, app_version, err)
        log("error", "daemon.config.invalid", "配置读取或校验失败，本轮跳过巡检", path=cfg_path, error=str(exc))
        return 1
    return run_daemon_once(cfg, cfg_path)


def run_daemon_loop(
    cfg_path: str,
    app_version: str,
    once: bool = False,
    interval: int | None = None,
) -> int:
    """执行 daemon（单轮或循环模式）。"""
    # 配置不可用时，使用“配置文件同级目录”的约定路径，不再回退到历史固定目录。
    lock_path = str(Path(cfg_path).resolve().parent / "locks" / "daemon.lock")
    interval_seconds = interval if interval is not None else 5
    try:
        cfg = _load_config_or_raise(cfg_path, app_version)
        lock_path = str((cfg.get("daemon", {}) or {}).get("lock_file", lock_path))
        if interval is None:
            interval_seconds = int((cfg.get("daemon", {}) or {}).get("interval_seconds", interval_seconds))
    except Exception:
        # 首次读取失败时保持默认锁和间隔，后续在每轮内部继续尝试。
        pass
    else:
        try:
            restored = restore_env_cache_to_process(cfg)
            log(
                "info",
                "daemon.env.restored",
                "daemon 启动时已恢复 service 环境变量",
                restored_env_keys=restored,
            )
        except Exception as exc:
            # 环境元信息恢复是 best-effort；失败不影响 daemon 巡检和上报从缓存单独取值。
            log("warn", "daemon.env.restore_failed", "daemon 恢复 service 环境变量失败，继续启动", error=str(exc))

    try:
        with FileLock(lock_path):
            if once:
                return _daemon_cycle(cfg_path, app_version)
            while True:
                try:
                    _daemon_cycle(cfg_path, app_version)
                except Exception as exc:
                    # 理论上 run_daemon_once 不应抛出；这里再兜底保证循环不退出。
                    log("error", "daemon.loop.iteration_error", "单轮执行抛出异常，继续下一轮", error=str(exc))
                time.sleep(interval_seconds)
    except BlockingIOError:
        log("warn", "daemon.lock.busy", "已有 daemon 实例在运行，当前退出")
        return 3
