from __future__ import annotations

"""daemon 自更新逻辑。"""

import errno
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from typing import Any

from .common import file_hash, log, run_command, shlex_quote
from .constants import APP_VERSION
from .paths import root_path, runtime_root_dir
from .runtime_profile import is_rootless_profile


@dataclass
class GuardUpdateResult:
    ok: bool
    updated: bool
    target_version: str
    error: str = ""


def _normalize_version(value: str) -> str:
    return str(value or "").strip()


def _override_package_ref_version(package_ref: str, target_version: str) -> str:
    ref = package_ref.strip()
    version = target_version.strip()
    if not ref or not version:
        return ref
    slash_pos = ref.rfind("/")
    colon_pos = ref.rfind(":")
    if colon_pos > slash_pos:
        return ref[: colon_pos + 1] + version
    return f"{ref}:{version}"


def _pick_package_file(pkg_dir: Path, preferred_name: str) -> Path | None:
    if preferred_name:
        target = pkg_dir / preferred_name
        if target.exists() and target.is_file():
            return target
    candidates = [p for p in pkg_dir.glob("*") if p.is_file()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _load_release_meta(meta_dir: Path, meta_file: str) -> dict[str, Any]:
    target = meta_dir / meta_file
    if not target.exists():
        raise RuntimeError(f"release meta missing: {target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"release meta invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("release meta invalid: root must be object")
    return payload


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    path.unlink()


def _copy_path(src: Path, dst: Path) -> None:
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, dst, symlinks=True)
        return
    shutil.copy2(src, dst, follow_symlinks=False)


def _move_path(src: Path, dst: Path, *, require_source_cleanup: bool = True) -> None:
    """Move a runtime entry, falling back when overlayfs rejects rename."""
    try:
        src.rename(dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise

    tmp = dst.with_name(f".{dst.name}.copying")
    _remove_path(tmp)
    _copy_path(src, tmp)
    try:
        tmp.rename(dst)
    except Exception:
        _remove_path(tmp)
        raise
    try:
        _remove_path(src)
    except Exception:
        if require_source_cleanup:
            raise


def _replace_runtime_entry(staged_root: Path, runtime_root: Path, relative_path: str) -> None:
    src = staged_root / relative_path
    if not src.exists():
        raise RuntimeError(f"bundle missing required path: {relative_path}")
    dst = runtime_root / relative_path
    backup = dst.with_name(dst.name + ".old")
    _remove_path(backup)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        _move_path(dst, backup)
    _move_path(src, dst, require_source_cleanup=False)
    _remove_path(backup)


def _restart_daemon_via_supervisor(cfg: dict[str, Any]) -> None:
    runtime_sup = (cfg.get("runtime", {}) or {}).get("supervisor", {}) or {}
    ctl_bin = str(runtime_sup["ctl_bin"])
    ctl_conf = runtime_sup.get("ctl_conf")
    program = str(runtime_sup.get("daemon_program", "sandbox-daemon"))
    base = f"{ctl_bin} -c {shlex_quote(str(ctl_conf))}" if ctl_conf else ctl_bin

    if not is_rootless_profile(cfg):
        reread = run_command(f"{base} reread", timeout=60)
        if reread.returncode != 0:
            raise RuntimeError(f"supervisor reread failed: {reread.stderr.strip() or reread.stdout.strip()}")

        update = run_command(f"{base} update {shlex_quote(program)}", timeout=60)
        if update.returncode != 0:
            log(
                "warn",
                "self_update.supervisor.update_failed",
                "supervisor update sandbox-daemon 失败，继续尝试 restart/start",
                program=program,
                error=(update.stderr.strip() or update.stdout.strip())[:200],
            )

    restart = run_command(f"{base} restart {shlex_quote(program)}", timeout=60)
    restart_text = f"{restart.stdout}\n{restart.stderr}".lower()
    if restart.returncode == 0:
        return
    if "not running" in restart_text or "no such process" in restart_text:
        start = run_command(f"{base} start {shlex_quote(program)}", timeout=60)
        start_text = f"{start.stdout}\n{start.stderr}".lower()
        if start.returncode == 0 or "already started" in start_text:
            return
        raise RuntimeError(f"supervisor start failed: {start.stderr.strip() or start.stdout.strip()}")
    raise RuntimeError(f"supervisor restart failed: {restart.stderr.strip() or restart.stdout.strip()}")


def ensure_guard_version(
    cfg: dict[str, Any],
    requested_target_version: str = "auto",
    current_version: str | None = None,
) -> GuardUpdateResult:
    current = _normalize_version(current_version or APP_VERSION)
    update_cfg = (cfg.get("self_update", {}) or {})
    if not isinstance(update_cfg, dict):
        return GuardUpdateResult(False, False, current, "self_update config must be object")

    oras_bin = str(update_cfg.get("oras_bin", "/usr/local/bin/oras")).strip()
    oras_host = str(update_cfg.get("oras_host", "")).strip()
    oras_user = str(update_cfg.get("oras_user", "")).strip()
    oras_password = str(update_cfg.get("oras_password", "")).strip()
    env_name = _normalize_version(str(update_cfg.get("environment", "DEV"))).upper() or "DEV"
    meta_ref = str(update_cfg.get("meta_ref", "")).strip()
    meta_file = str(update_cfg.get("meta_file", "release.json")).strip() or "release.json"
    work_root = Path(str(update_cfg.get("work_dir") or root_path(cfg, "work", "self_update"))).expanduser()
    runtime_root = Path(runtime_root_dir(cfg)).expanduser()

    if not oras_host or not oras_user or not oras_password or not meta_ref:
        return GuardUpdateResult(False, False, current, "self_update config missing required oras_host/oras_user/oras_password/meta_ref")

    log(
        "info",
        "self_update.start",
        "开始执行 daemon 自更新",
        current_version=current,
        requested_target_version=requested_target_version,
        environment=env_name,
    )
    work_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run_", dir=str(work_root)))
    meta_dir = run_dir / "meta"
    pkg_dir = run_dir / "pkg"
    stage_dir = run_dir / "stage"
    meta_dir.mkdir(parents=True, exist_ok=True)
    pkg_dir.mkdir(parents=True, exist_ok=True)
    stage_dir.mkdir(parents=True, exist_ok=True)

    try:
        login = run_command(
            f"{shlex_quote(oras_bin)} login --plain-http {shlex_quote(oras_host)} -u {shlex_quote(oras_user)} -p {shlex_quote(oras_password)}",
            timeout=30,
        )
        if login.returncode != 0:
            return GuardUpdateResult(False, False, current, f"oras login failed: {login.stderr.strip() or login.stdout.strip()}")

        meta_pull = run_command(
            f"cd {shlex_quote(str(meta_dir))} && {shlex_quote(oras_bin)} pull --plain-http {shlex_quote(meta_ref)}",
            timeout=120,
        )
        if meta_pull.returncode != 0:
            return GuardUpdateResult(False, False, current, f"meta pull failed: {meta_pull.stderr.strip() or meta_pull.stdout.strip()}")

        try:
            release = _load_release_meta(meta_dir, meta_file)
        except Exception as exc:
            return GuardUpdateResult(False, False, current, str(exc))
        target_version = _normalize_version(str(release.get("version", "")))
        package_ref = str(release.get("package", "")).strip()
        package_file_name = str(release.get("file", "")).strip()
        sha256_expect = _normalize_version(str(release.get("sha256", ""))).lower()

        requested = _normalize_version(requested_target_version)
        if requested and requested != "auto":
            target_version = requested
            package_ref = _override_package_ref_version(package_ref, requested)
            sha256_expect = ""
            log("info", "self_update.force_version", "使用指定版本进行 daemon 自更新", target_version=target_version, environment=env_name)
        if not target_version or not package_ref:
            return GuardUpdateResult(False, False, current, "release meta missing required version/package")

        if current == target_version:
            log(
                "info",
                "self_update.skip_latest",
                "当前已是最新版本，跳过自更新",
                current_version=current,
                target_version=target_version,
                environment=env_name,
            )
            return GuardUpdateResult(True, False, target_version, "")

        pkg_pull = run_command(
            f"cd {shlex_quote(str(pkg_dir))} && {shlex_quote(oras_bin)} pull --plain-http {shlex_quote(package_ref)}",
            timeout=180,
        )
        if pkg_pull.returncode != 0:
            return GuardUpdateResult(False, False, target_version, f"package pull failed: {pkg_pull.stderr.strip() or pkg_pull.stdout.strip()}")

        package_path = _pick_package_file(pkg_dir, package_file_name)
        if package_path is None:
            return GuardUpdateResult(False, False, target_version, "bundle package missing after pull")
        if sha256_expect:
            actual = file_hash(package_path).lower()
            if actual != sha256_expect:
                return GuardUpdateResult(False, False, target_version, f"bundle sha256 mismatch: expect={sha256_expect} actual={actual}")

        log(
            "info",
            "self_update.package_ready",
            "自更新包已就绪",
            target_version=target_version,
            package=str(package_path),
            environment=env_name,
        )
        try:
            with tarfile.open(package_path, "r:gz") as tf:
                tf.extractall(stage_dir)
        except Exception as exc:
            return GuardUpdateResult(False, False, target_version, f"bundle extract failed: {exc}")

        try:
            _replace_runtime_entry(stage_dir, runtime_root, "sandbox_guard.py")
            _replace_runtime_entry(stage_dir, runtime_root, "sandbox_reconcile")
            _replace_runtime_entry(stage_dir, runtime_root, "scripts")
            _replace_runtime_entry(stage_dir, runtime_root, "config.json")
        except Exception as exc:
            return GuardUpdateResult(False, False, target_version, str(exc))

        return GuardUpdateResult(True, True, target_version, "")
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def run_self_update(
    cfg: dict[str, Any],
    requested_target_version: str = "auto",
    current_version: str | None = None,
) -> int:
    result = ensure_guard_version(cfg, requested_target_version, current_version)
    current = _normalize_version(current_version or APP_VERSION)
    env_name = _normalize_version(str((cfg.get("self_update", {}) or {}).get("environment", "DEV"))).upper() or "DEV"

    if not result.ok:
        raise RuntimeError(result.error or "guard update failed")
    if not result.updated:
        return 0

    log("info", "self_update.restart_daemon", "daemon 代码替换完成，开始重启", target_version=result.target_version, environment=env_name)
    _restart_daemon_via_supervisor(cfg)
    log("info", "self_update.success", "daemon 自更新完成", target_version=result.target_version, environment=env_name)
    if current == result.target_version:
        return 0
    return 0
