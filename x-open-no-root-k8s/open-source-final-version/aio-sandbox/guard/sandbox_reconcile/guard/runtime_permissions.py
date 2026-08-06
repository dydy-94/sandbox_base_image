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


def _has_foreign_owned_entry(path: Path, expected_uid: int) -> bool:
    """检查 Guard 管理路径内是否混入其他用户创建的文件或目录。"""
    if not path.exists() and not path.is_symlink():
        return False
    try:
        if path.lstat().st_uid != expected_uid:
            return True
        if not path.is_dir() or path.is_symlink():
            return False
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in [*dirs, *files]:
                candidate = Path(root) / name
                if candidate.lstat().st_uid != expected_uid:
                    return True
    except (OSError, PermissionError):
        return True
    return False


def _self_update_paths(cfg: dict[str, Any]) -> list[Path]:
    root_dir = Path(str((cfg.get("runtime", {}) or {}).get("root_dir") or "/home/x/.daemon")).expanduser()
    names = ["sandbox_guard.py", "sandbox_reconcile", "scripts", "config.json"]
    result: list[Path] = []
    for name in names:
        target = root_dir / name
        result.extend([target, target.with_name(f"{target.name}.old"), target.with_name(f".{target.name}.copying")])
    return result


def ensure_rootless_self_update_permissions(cfg: dict[str, Any]) -> None:
    """修复 root 探测 Python 后遗留的 pycache，保证 x 可原子替换 Guard。"""
    if not is_rootless_profile(cfg):
        return
    runtime = cfg.get("runtime", {}) or {}
    execution_user = str(runtime.get("execution_user") or "x")
    try:
        account = pwd.getpwnam(execution_user)
    except KeyError as exc:
        raise RuntimeError(f"rootless execution user not found: {execution_user}") from exc

    root_dir = Path(str(runtime.get("root_dir") or "/home/x/.daemon")).expanduser()
    try:
        root_dir.relative_to(Path("/home/x"))
    except ValueError as exc:
        raise RuntimeError(f"rootless repair path outside /home/x: {root_dir}") from exc

    paths = [
        path
        for path in _self_update_paths(cfg)
        if _has_foreign_owned_entry(path, account.pw_uid)
    ]
    if not paths:
        return

    quoted = " ".join(shlex_quote(str(path)) for path in paths)
    log(
        "warn",
        "self_update.rootless.permission_repair_start",
        "Guard 程序目录包含非运行用户文件，尝试 sudo 修复",
        paths=[str(path) for path in paths],
    )
    result = run_command(
        f"sudo -n chown -R {shlex_quote(execution_user)}:{shlex_quote(execution_user)} {quoted}",
        timeout=120,
    )
    remaining = [
        path
        for path in paths
        if _has_foreign_owned_entry(path, account.pw_uid)
    ]
    log(
        "info" if result.returncode == 0 and not remaining else "warn",
        "self_update.rootless.permission_repair_done",
        "Guard 程序目录 sudo 修复完成",
        returncode=result.returncode,
        paths=[str(path) for path in paths],
        remaining=[str(path) for path in remaining],
    )
    if remaining:
        error = result.stderr.strip() or result.stdout.strip()
        detail = f": {error}" if error else ""
        raise RuntimeError(f"rootless self-update paths remain owned by another user{detail}")


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
