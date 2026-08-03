from __future__ import annotations

"""code-server 专用包升级流程。

依赖安装和双层压缩包部署只在版本变化时执行；日常 daemon 恢复仍由 PM2
策略负责，不会重复执行 apt、pipx 或文件替换。
"""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any
import uuid

from .common import log, run_command, shlex_quote
from .process_applicability import process_applicability
from .strategy.factory import get_strategy
from .types import CommandResult
from .workdir_cleanup import cleanup_current_upgrade_workdirs, cleanup_upgrade_workdir


def _normalize_version(value: str) -> str:
    return str(value).replace("\ufeff", "").strip()


def _override_ref_version(package_ref: str, target_version: str) -> str:
    ref = package_ref.strip()
    version = target_version.strip()
    slash_pos = ref.rfind("/")
    colon_pos = ref.rfind(":")
    if colon_pos > slash_pos:
        return ref[: colon_pos + 1] + version
    return f"{ref}:{version}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        while chunk := fp.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _command(cmd: str, *, timeout: int, env: dict[str, str] | None = None) -> CommandResult:
    try:
        return run_command(cmd, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"command timeout after {timeout} seconds")


def _result_error(result: CommandResult, fallback: str) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or fallback
    return detail[-4000:]


def _load_version(path: Path) -> str:
    if not path.exists():
        return ""
    return _normalize_version(path.read_text(encoding="utf-8"))


def _save_version_atomic(path: Path, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    temp.write_text(_normalize_version(version), encoding="utf-8")
    temp.replace(path)


def _clear_directory_files(directory: Path) -> None:
    if not directory.exists():
        return
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _pm2_delete_not_found(result: CommandResult) -> bool:
    text = f"{result.stdout}\n{result.stderr}".lower()
    return any(
        marker in text
        for marker in ["not found", "does not exist", "doesn't exist", "process or namespace not found"]
    )


def _run_pre_recover_command(proc: dict[str, Any]) -> tuple[bool, str]:
    cmd = str(proc.get("pre_recover_command", "")).strip()
    if not cmd:
        return True, ""
    result = _command(cmd, timeout=30)
    if result.returncode == 0:
        return True, ""
    return False, _result_error(result, "pre_recover_command failed")


def _clear_pm2_probe_cache(cfg: dict[str, Any]) -> None:
    cache = cfg.get("_pm2_probe_cache")
    if isinstance(cache, dict):
        cache.clear()


def _wait_until_online(
    proc: dict[str, Any],
    cfg: dict[str, Any],
    *,
    timeout_seconds: float,
    interval_seconds: float,
) -> tuple[bool, str]:
    strategy = get_strategy(str(proc.get("manager", "pm2")))
    deadline = time.monotonic() + timeout_seconds
    last_message = "process not online"
    while True:
        _clear_pm2_probe_cache(cfg)
        probe = strategy.probe(proc, cfg)
        if probe.exists and probe.running:
            return True, ""
        last_message = f"{probe.raw_status}: {probe.message}"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, f"start failed: {last_message}"
        time.sleep(min(interval_seconds, remaining))


def _start_and_confirm(proc: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, str]:
    up = proc.get("upgrade", {}) or {}
    wait_cfg = up.get("ready_wait", {}) or {}
    timeout_seconds = max(0.1, float(wait_cfg.get("timeout_seconds", 15)))
    interval_seconds = max(0.05, float(wait_cfg.get("interval_seconds", 0.3)))
    pre_ok, pre_error = _run_pre_recover_command(proc)
    if not pre_ok:
        return False, pre_error
    strategy = get_strategy(str(proc.get("manager", "pm2")))
    result = strategy.start(proc, cfg)
    if result.returncode != 0:
        return False, _result_error(result, "start failed")
    return _wait_until_online(
        proc,
        cfg,
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
    )


def _ensure_current_process(proc: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, str]:
    strategy = get_strategy(str(proc.get("manager", "pm2")))
    _clear_pm2_probe_cache(cfg)
    probe = strategy.probe(proc, cfg)
    if probe.exists and probe.running:
        return True, ""
    if probe.exists:
        deleted = strategy.delete(proc, cfg)
        if deleted.returncode != 0 and not _pm2_delete_not_found(deleted):
            return False, f"stop failed: {_result_error(deleted, 'unable to delete stale pm2 process')}"
    log(
        "info",
        "upgrade.code_server.ensure_process",
        "当前已是目标版本，确保 code-server 已启动",
        process=str(proc.get("name", "code-server")),
        probe_status=probe.raw_status,
        probe_message=probe.message,
    )
    return _start_and_confirm(proc, cfg)


def _install_dependencies(up: dict[str, Any]) -> tuple[bool, str]:
    timeout = max(1, int(up.get("dependency_timeout_seconds", 1800)))
    command = """
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y pipx
pipx ensurepath
if [ -f /home/x/.bashrc ]; then
  source /home/x/.bashrc
fi
pipx install pyright
pipx install ruff
pipx install black
pipx install 'python-lsp-server[all]'
""".strip()
    result = _command(f"bash -c {shlex_quote(command)}", timeout=timeout)
    if result.returncode == 0:
        return True, ""
    return False, f"dependency install failed: {_result_error(result, 'unknown error')}"


def _install_go_tools(outer_root: Path, go_bin_dir: Path) -> tuple[bool, str]:
    go_bin_dir.mkdir(parents=True, exist_ok=True)
    command = (
        f"mv -f {shlex_quote(str(outer_root / 'gopls'))} {shlex_quote(str(go_bin_dir / 'gopls'))} && "
        f"mv -f {shlex_quote(str(outer_root / 'goimports'))} {shlex_quote(str(go_bin_dir / 'goimports'))} && "
        f"chmod 755 {shlex_quote(str(go_bin_dir / 'gopls'))} {shlex_quote(str(go_bin_dir / 'goimports'))}"
    )
    result = _command(command, timeout=60)
    if result.returncode == 0:
        return True, ""
    return False, f"go tools install failed: {_result_error(result, 'unknown error')}"


def _deploy_code_server(
    proc: dict[str, Any],
    cfg: dict[str, Any],
    *,
    inner_package: Path,
    deploy_root: Path,
    inner_root_name: str,
) -> tuple[bool, str]:
    if not deploy_root.is_absolute() or deploy_root == Path("/"):
        return False, f"unsafe deploy root: {deploy_root}"
    strategy = get_strategy(str(proc.get("manager", "pm2")))
    stop = strategy.delete(proc, cfg)
    if stop.returncode != 0 and not _pm2_delete_not_found(stop):
        return False, f"stop failed: {_result_error(stop, 'unknown error')}"
    pre_ok, pre_error = _run_pre_recover_command(proc)
    if not pre_ok:
        return False, pre_error

    deploy_root.mkdir(parents=True, exist_ok=True)
    clean = _command(f"rm -rf {shlex_quote(str(deploy_root))}/*", timeout=300)
    if clean.returncode != 0:
        return False, f"prepare deploy failed: {_result_error(clean, 'clean deploy root failed')}"
    extract_timeout = max(1, int(((proc.get("upgrade", {}) or {}).get("deploy_timeout_seconds", 300))))
    extract = _command(
        f"unzip -q -o {shlex_quote(str(inner_package))} -d {shlex_quote(str(deploy_root))}",
        timeout=extract_timeout,
    )
    if extract.returncode != 0:
        return False, f"extract failed: {_result_error(extract, 'inner package unzip failed')}"

    executable = deploy_root / inner_root_name / "bin" / "code-server"
    node = deploy_root / inner_root_name / "lib" / "node"
    if not executable.is_file() or executable.stat().st_size <= 0:
        return False, f"package layout invalid: executable missing or empty: {executable}"
    if not node.is_file() or node.stat().st_size <= 0:
        return False, f"package layout invalid: node missing or empty: {node}"
    chmod = _command(
        f"chmod 755 {shlex_quote(str(executable))} {shlex_quote(str(node))}",
        timeout=30,
    )
    if chmod.returncode != 0:
        return False, f"prepare deploy failed: {_result_error(chmod, 'chmod failed')}"
    return _start_and_confirm(proc, cfg)


def execute_code_server_package_upgrade(
    proc: dict[str, Any],
    cfg: dict[str, Any],
    requested_target_version: str = "auto",
) -> tuple[bool, str, str, bool]:
    """执行 code-server 的 meta、依赖安装、双层包部署和 PM2 启动流程。"""
    up = proc.get("upgrade", {}) or {}
    required = [
        "oras_bin",
        "oras_host",
        "oras_user",
        "oras_password",
        "meta_ref",
        "outer_root",
        "inner_package_file",
        "inner_root",
        "deploy_root",
        "go_bin_dir",
        "current_version_file",
    ]
    for key in required:
        if not str(up.get(key, "")).strip():
            return False, f"upgrade.{key} 为空", "", False
    if str(proc.get("manager", "pm2")).strip() != "pm2":
        return False, "code_server_package requires pm2 manager", "", False

    name = str(proc.get("name", "code-server"))
    oras_bin = Path(str(up["oras_bin"]))
    if not oras_bin.exists():
        return False, f"oras binary missing: {oras_bin}", "", False

    root_dir = Path(str((cfg.get("runtime", {}) or {}).get("root_dir", "/home/x/.daemon")))
    work_dir = Path(str(up.get("work_dir", root_dir / "work" / name)))
    cleanup_upgrade_workdir(work_dir, min_age_seconds=0, process=name, reason="upgrade_preflight")
    meta_dir = work_dir / "meta"
    pkg_dir = work_dir / "pkg"
    stage_dir = work_dir / f"stage_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    for directory in [meta_dir, pkg_dir, stage_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    target_version = ""
    try:
        log("info", "upgrade.code_server.start", "开始执行 code-server 升级流程", process=name)
        login = _command(
            f"{shlex_quote(str(oras_bin))} login --plain-http {shlex_quote(str(up['oras_host']))} "
            f"-u {shlex_quote(str(up['oras_user']))} -p {shlex_quote(str(up['oras_password']))}",
            timeout=60,
        )
        if login.returncode != 0:
            return False, f"oras login failed: {_result_error(login, 'unknown error')}", "", False

        _clear_directory_files(meta_dir)
        meta_ref = str(up["meta_ref"])
        meta_pull = _command(
            f"cd {shlex_quote(str(meta_dir))} && {shlex_quote(str(oras_bin))} "
            f"pull --plain-http {shlex_quote(meta_ref)}",
            timeout=120,
        )
        if meta_pull.returncode != 0:
            return False, f"meta pull failed: {_result_error(meta_pull, 'unknown error')}", "", False

        meta_file = meta_dir / str(up.get("meta_file", "release.json"))
        if not meta_file.is_file():
            return False, f"meta file missing: {meta_file}", "", False
        try:
            release = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception as exc:
            return False, f"release json parse failed: {exc}", "", False
        if not isinstance(release, dict):
            return False, "release json must be object", "", False

        target_version = _normalize_version(str(release.get("version", "")))
        package_ref = str(release.get("package", "")).strip()
        package_file_name = str(release.get("file", "")).strip()
        sha256_expected = str(release.get("sha256", "")).strip().lower()
        requested_version = _normalize_version(requested_target_version) or "auto"
        if requested_version.lower() != "auto":
            target_version = requested_version
            package_ref = _override_ref_version(package_ref, requested_version)
            sha256_expected = ""
        if not target_version or not package_ref:
            return False, "release json missing required fields: version/package", "", False

        version_file = Path(str(up["current_version_file"]))
        current_version = _load_version(version_file)
        log(
            "info",
            "upgrade.code_server.version_compare",
            "code-server 版本比较",
            process=name,
            current_version=current_version,
            target_version=target_version,
        )
        if current_version and current_version == target_version:
            ok, error = _ensure_current_process(proc, cfg)
            return ok, error, target_version, True

        _clear_directory_files(pkg_dir)
        package_pull = _command(
            f"cd {shlex_quote(str(pkg_dir))} && {shlex_quote(str(oras_bin))} "
            f"pull --plain-http {shlex_quote(package_ref)}",
            timeout=300,
        )
        if package_pull.returncode != 0:
            return False, f"package pull failed: {_result_error(package_pull, 'unknown error')}", target_version, False
        package_path = pkg_dir / package_file_name
        if not package_file_name or not package_path.is_file():
            return False, f"package file missing: {package_path}", target_version, False
        if sha256_expected:
            sha256_actual = _sha256_file(package_path).lower()
            if sha256_actual != sha256_expected:
                return (
                    False,
                    f"sha256 mismatch: expect={sha256_expected} actual={sha256_actual}",
                    target_version,
                    False,
                )

        extract = _command(
            f"unzip -q -o {shlex_quote(str(package_path))} -d {shlex_quote(str(stage_dir))}",
            timeout=max(1, int(up.get("deploy_timeout_seconds", 300))),
        )
        if extract.returncode != 0:
            return False, f"extract failed: {_result_error(extract, 'outer package unzip failed')}", target_version, False

        outer_root = stage_dir / str(up["outer_root"])
        inner_package = outer_root / str(up["inner_package_file"])
        required_files = [inner_package, outer_root / "gopls", outer_root / "goimports"]
        missing = [str(path) for path in required_files if not path.is_file() or path.stat().st_size <= 0]
        if missing:
            return False, f"package layout invalid: missing or empty: {', '.join(missing)}", target_version, False

        dependencies_ok, dependency_error = _install_dependencies(up)
        if not dependencies_ok:
            return False, dependency_error, target_version, False

        applicability = process_applicability(proc, cfg)
        if not applicability.applicable:
            log(
                "info",
                "upgrade.code_server.skip_inapplicable_before_deploy",
                "部署前进程已变为不适用，取消本次文件替换",
                process=name,
                reason=applicability.reason,
            )
            return True, "", target_version, True

        tools_ok, tools_error = _install_go_tools(outer_root, Path(str(up["go_bin_dir"])))
        if not tools_ok:
            return False, tools_error, target_version, False
        deployed, deploy_error = _deploy_code_server(
            proc,
            cfg,
            inner_package=inner_package,
            deploy_root=Path(str(up["deploy_root"])),
            inner_root_name=str(up["inner_root"]),
        )
        if not deployed:
            return False, deploy_error, target_version, False
        _save_version_atomic(version_file, target_version)
        return True, "", target_version, False
    finally:
        cleanup_current_upgrade_workdirs(stage_dir, pkg_dir, process=name)
