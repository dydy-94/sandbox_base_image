from __future__ import annotations

"""rootless 运行目录校验与首期 sudo 兼容修复。"""

import os
from pathlib import Path
import pwd
from typing import Any

from .common import log, run_command, shlex_quote
from .runtime_profile import is_rootless_profile


def _required_dirs(cfg: dict[str, Any]) -> list[Path]:
    runtime = cfg.get("runtime", {}) or {}
    candidates = [
        Path(str(runtime.get("root_dir") or "/home/x/.daemon")),
        Path(str(runtime.get("tmp_dir") or "/home/x/tmp")),
        Path(str(runtime.get("pm2_home") or "/home/x/.pm2")),
        Path(str(runtime.get("env_cache_file") or "/home/x/.data/sandbox_guard_env.json")).parent,
    ]
    result: list[Path] = []
    for path in candidates:
        expanded = path.expanduser()
        if expanded not in result:
            result.append(expanded)
    return result


def _is_writable_dir(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK | os.X_OK)


def ensure_rootless_runtime_dirs(cfg: dict[str, Any]) -> None:
    if not is_rootless_profile(cfg):
        return
    execution_user = str((cfg.get("runtime", {}) or {}).get("execution_user") or "x")
    try:
        account = pwd.getpwnam(execution_user)
    except KeyError as exc:
        raise RuntimeError(f"rootless execution user not found: {execution_user}") from exc
    if os.geteuid() != account.pw_uid:
        raise RuntimeError(
            f"rootless guard must run as {execution_user} uid={account.pw_uid}, current uid={os.geteuid()}"
        )

    paths = _required_dirs(cfg)
    allowed_root = Path("/home/x")
    for path in paths:
        try:
            path.relative_to(allowed_root)
        except ValueError as exc:
            raise RuntimeError(f"rootless repair path outside /home/x: {path}") from exc
        try:
            path.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass

    failed = [path for path in paths if not _is_writable_dir(path)]
    if failed:
        quoted = " ".join(shlex_quote(str(path)) for path in failed)
        log(
            "warn",
            "bootstrap.rootless.permission_repair_start",
            "rootless 必需目录不可写，尝试 sudo 修复",
            paths=[str(path) for path in failed],
        )
        install = run_command(
            f"sudo -n install -d -o {shlex_quote(execution_user)} -g {shlex_quote(execution_user)} {quoted}",
            timeout=60,
        )
        chown = run_command(
            f"sudo -n chown -R {shlex_quote(execution_user)}:{shlex_quote(execution_user)} {quoted}",
            timeout=300,
        )
        log(
            "info" if install.returncode == 0 and chown.returncode == 0 else "warn",
            "bootstrap.rootless.permission_repair_done",
            "rootless 必需目录 sudo 修复完成",
            install_returncode=install.returncode,
            chown_returncode=chown.returncode,
            paths=[str(path) for path in failed],
        )

    remaining = [path for path in paths if not _is_writable_dir(path)]
    if remaining:
        raise RuntimeError(f"rootless required directories are not writable: {', '.join(str(path) for path in remaining)}")
