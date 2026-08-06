from __future__ import annotations

"""guard 启动入口。"""

import os
import sys
import time
from pathlib import Path
from typing import Any

from .activation import runtime_config_path, write_activation_pending
from .common import FileLock, log
from .config import apply_defaults, read_config, validate_config
from .constants import APP_VERSION
from .env_store import bootstrap_lock_file, restore_env_cache_to_process
from .runtime_permissions import ensure_rootless_runtime_dirs
from .self_update import ensure_guard_version
from .startup_timing import initialize_startup_timing_for_bootstrap


def _prepare_entry_env(
    *,
    sandbox_started_at_ms: str | None,
    from_launcher: bool,
) -> None:
    os.environ["SANDBOX_GUARD_MIRROR_PID1"] = "0" if from_launcher else "1"
    os.environ["SANDBOX_GUARD_BOOTSTRAP_STARTED_AT_MS"] = str(int(time.time() * 1000))
    os.environ["SANDBOX_GUARD_BOOTSTRAP_SOURCE"] = "launcher" if from_launcher else "manual"
    sandbox_started = str(sandbox_started_at_ms or "").strip()
    if sandbox_started:
        os.environ["SANDBOX_GUARD_SANDBOX_STARTED_AT_MS"] = sandbox_started


def _bootstrap_script_path(cfg: dict[str, Any], cfg_path: str) -> str:
    runtime_root = str((cfg.get("runtime", {}) or {}).get("root_dir", "")).strip()
    if runtime_root:
        return str(Path(runtime_root).expanduser() / "sandbox_guard.py")
    return str(Path(cfg_path).expanduser().resolve().parent / "sandbox_guard.py")


def _exec_bootstrap(cfg: dict[str, Any], cfg_path: str) -> int:
    script = _bootstrap_script_path(cfg, cfg_path)
    os.execv(sys.executable, [sys.executable, script, "bootstrap", "--config", cfg_path])
    return 1


def run_bootstrap_entry(
    cfg: dict[str, Any],
    cfg_path: str,
    *,
    sandbox_started_at_ms: str = "",
    user_id: str = "",
    user_name: str = "",
    base_url: str = "",
    auth_token: str = "",
    target_version: str = "auto",
    from_launcher: bool = False,
) -> int:
    ensure_rootless_runtime_dirs(cfg)
    initialize_startup_timing_for_bootstrap(cfg)
    _prepare_entry_env(
        sandbox_started_at_ms=sandbox_started_at_ms,
        from_launcher=from_launcher,
    )

    lock_path = bootstrap_lock_file(cfg)
    try:
        with FileLock(lock_path, inherit_on_exec=True):
            restored = restore_env_cache_to_process(cfg)
            log(
                "info",
                "bootstrap.entry.start",
                "bootstrap-entry 已获取启动锁",
                source=os.environ.get("SANDBOX_GUARD_BOOTSTRAP_SOURCE", "manual"),
                restored_env_keys=restored,
            )
            result = ensure_guard_version(cfg, requested_target_version=target_version)
            if result.ok and result.updated:
                log("info", "bootstrap.entry.guard_updated", "启动前 guard 更新完成，切换到新版本继续启动", target_version=result.target_version)
            elif not result.ok:
                log("warn", "bootstrap.entry.update_degraded", "启动前 guard 更新失败，继续使用本地版本启动", error=result.error)
            return _exec_bootstrap(cfg, cfg_path)
    except BlockingIOError:
        log(
            "warn",
            "bootstrap.lock.busy",
            "已有 bootstrap 主链路在运行，当前 bootstrap-entry 退出",
            source=os.environ.get("SANDBOX_GUARD_BOOTSTRAP_SOURCE", "manual"),
        )
        return 3


def run_launcher(cfg_path: str) -> int:
    """镜像内置 guard 的一次性启动器。

    launcher 不轮询、不阻塞 AIO 主启动。若 config 缺失，写 pending 标识并退出，
    等待上层下发 config 后由 update-env 或后续激活命令触发 bootstrap-entry。
    """
    config_path = Path(runtime_config_path(None, cfg_path)).expanduser()
    if not config_path.exists():
        marker_cfg = {"runtime": {"root_dir": str(config_path.parent)}}
        write_activation_pending(marker_cfg, reason="config_missing")
        log(
            "info",
            "launcher.activation.pending",
            "guard config 缺失，launcher 写入 pending 标识后退出",
            config=str(config_path),
        )
        return 0

    try:
        cfg = apply_defaults(read_config(str(config_path)), APP_VERSION)
        errors = validate_config(cfg)
        if errors:
            raise ValueError("; ".join(errors))
    except Exception as exc:
        log("error", "launcher.config.invalid", "launcher 读取 config 失败", config=str(config_path), error=str(exc))
        return 1
    return run_bootstrap_entry(cfg, str(config_path), from_launcher=True)
