from __future__ import annotations

"""service 环境变量下发入口。"""

from typing import Any

from .activation import consume_activation_pending, has_activation_pending, schedule_bootstrap_entry, start_launcher_via_supervisor
from .common import FileLock, log
from .env_store import (
    append_xagent_env_changed_unlocked,
    build_service_env_from_args,
    changed_service_dynamic_keys,
    env_lock_file,
    read_env_cache,
    write_env_cache,
)
from .reconcile.env import build_managed_env_items
from .report import REPORT_STARTUP_TIMING, append_report_request
from .runtime_profile import is_rootless_profile
from .startup_timing import record_update_env_startup_timing


def run_update_env(cfg: dict[str, Any], args: dict[str, str | None], cfg_path: str | None = None) -> int:
    """写入 service 下发 env cache 和 .bashrc。

    update-env 不探测 PM2，不直接重启 xagent。
    仅当镜像内置 launcher 留下 activation pending 标识时，才 best-effort
    调度一次 bootstrap-entry；普通老路径无 pending 标识，行为保持不变。
    """
    lock_path = env_lock_file(cfg)
    with FileLock(lock_path):
        old_cache = read_env_cache(cfg)
        new_cache = build_service_env_from_args(cfg, args, old_cache)
        write_env_cache(cfg, new_cache)

        managed_items = build_managed_env_items(cfg, phase="update-env", env_cache=new_cache)
        if managed_items:
            from .reconcile.env import _upsert_managed_env_block

            env_file = str((cfg.get("runtime", {}) or {}).get("env_file", "/home/x/.bashrc"))
            _upsert_managed_env_block(env_file, managed_items)

        changed = changed_service_dynamic_keys(cfg, old_cache, new_cache)
        if changed:
            req = append_xagent_env_changed_unlocked(cfg, changed)
            log(
                "info",
                "env.update.xagent_env_changed",
                "service 动态环境变量发生变化，已追加 xagent env request",
                keys=changed,
                request_id=req.get("request_id"),
            )
        else:
            log(
                "info",
                "env.update.applied",
                "service 动态环境变量已写入",
                keys=sorted(new_cache.keys()),
                changed_keys=changed,
                request_created=bool(changed),
            )
        timing_recorded = record_update_env_startup_timing(
            cfg,
            startup_type=args.get("startup_type"),
            sandbox_startup_ms=args.get("sandbox_startup_duration_ms"),
            sandbox_init_ms=args.get("sandbox_init_duration_ms"),
        )
        if timing_recorded:
            append_report_request(cfg, REPORT_STARTUP_TIMING, reason="startup_timing")
            log(
                "info",
                "env.update.startup_timing_recorded",
                "启动时间上报初始信息已记录",
                startup_type=str(args.get("startup_type") or "").strip().upper(),
            )
    if cfg_path and has_activation_pending(cfg):
        activated = (
            start_launcher_via_supervisor(cfg)
            if is_rootless_profile(cfg)
            else schedule_bootstrap_entry(cfg, cfg_path, from_launcher=False)
        )
        if activated:
            consume_activation_pending(cfg)
    return 0
