from __future__ import annotations

"""升级任务调度与执行模块。

当前保留三类包升级策略：
- meta_package：memory-storage、nacos-heartbeat 等进程使用的通用包升级方案
- xagent_package：xagent 专用升级方案，当前主线重点维护对象
- code_server_package：code-server 依赖安装与双层压缩包部署方案
"""

import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any
import uuid

from .common import FileLock, ensure_parent, log, now_iso, prepare_async_child_env, run_command, shlex_quote
from .process_applicability import process_applicability
from .resources import current_disk_free_bytes
from .report import REPORT_UPGRADE_FAILED, append_report_request
from .startup_timing import merge_xagent_startup_timing
from .strategy.factory import get_strategy
from .strategy.pm2 import PM2_SKIP_LATEST_START_TIMEOUT_SECONDS
from .workdir_cleanup import cleanup_current_upgrade_workdirs, cleanup_upgrade_workdir
from .xagent_status import (
    XAGENT_STATUS_ABNORMAL,
    XAGENT_STATUS_CHECKING_UPDATE,
    XAGENT_STATUS_STARTING,
    XAGENT_STATUS_UPGRADING,
    clear_xagent_status,
    write_xagent_status,
)
from .xagent_package import detect_installed_xagent_mode, detect_staged_xagent_package

XAGENT_READY_WAIT_TIMEOUT_SECONDS = 15.0
XAGENT_READY_WAIT_INTERVAL_SECONDS = 0.3
XAGENT_READY_REQUEST_TIMEOUT_SECONDS = 1.0
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
_MB = 1024 * 1024
_XAGENT_STAGE_PREFIX = ".xagent-upgrade-stage"


def _now_ms() -> int:
    return int(time.time() * 1000)


def summarize_upgrade_failure(error: str) -> str:
    text = str(error or "").strip().lower()
    if not text:
        return "升级失败"
    if "insufficient free disk" in text or "no space left" in text or "磁盘" in text:
        return "磁盘不足"
    if "sha256" in text or "checksum" in text or "校验" in text:
        return "校验失败"
    if "oras binary missing" in text or ("upgrade." in text and "为空" in text) or "unsupported upgrade strategy" in text:
        return "配置错误"
    if "oras login failed" in text or "unauthorized" in text or "forbidden" in text or "authentication" in text:
        return "登录失败"
    if "meta pull failed" in text:
        return "元数据拉取失败"
    if "meta file missing" in text or "release json" in text or "missing required fields" in text:
        return "元数据异常"
    if "package pull failed" in text:
        return "升级包拉取失败"
    if (
        "package file missing" in text
        or "package layout invalid" in text
        or "package shape unsupported" in text
        or "source root missing" in text
        or "binary missing" in text
        or "binary empty" in text
    ):
        return "包结构异常"
    if "extract failed" in text or "unzip" in text:
        return "解压失败"
    if "dependency install failed" in text or "go tools install failed" in text:
        return "依赖安装失败"
    if "replace failed" in text or "prepare deploy failed" in text:
        return "替换失败"
    if "start failed" in text or "pre_recover_command failed" in text:
        return "启动失败"
    if "process not found" in text:
        return "进程不存在"
    return "升级失败"


def _append_upgrade_failed_report(cfg: dict[str, Any], proc_name: str, target_version: str, error: str, failed_at_ms: int) -> None:
    append_report_request(
        cfg,
        REPORT_UPGRADE_FAILED,
        reason="upgrade_failed",
        payload={
            "processName": str(proc_name or ""),
            "targetVersion": str(target_version or ""),
            "failureReason": summarize_upgrade_failure(error),
            "failedAtMs": failed_at_ms,
        },
    )


def load_upgrade_events(path: str) -> list[dict[str, Any]]:
    """读取并消费升级事件文件。"""
    p = Path(path)
    if not p.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        p.write_text("", encoding="utf-8")
    except Exception:
        return []
    return events


def append_upgrade_event(path: str, event: dict[str, Any]) -> None:
    """追加一条升级事件。"""
    ensure_parent(path)
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_upgrade_requests(path: str) -> list[dict[str, Any]]:
    """读取并消费强制升级请求文件。"""
    p = Path(path)
    if not p.exists():
        return []
    reqs: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                reqs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        p.write_text("", encoding="utf-8")
    except Exception:
        return []
    return reqs


def append_upgrade_request(
    path: str,
    process: str,
    reason: str = "external_trigger",
    target_version: str = "auto",
) -> None:
    """追加一条强制升级请求。"""
    ensure_parent(path)
    payload = {
        "process": process,
        "reason": reason,
        "target_version": target_version,
        "requested_at": now_iso(),
        "request_id": str(uuid.uuid4()),
    }
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _min_free_disk_mb(upgrade_cfg: dict[str, Any]) -> int:
    raw = upgrade_cfg.get("min_free_disk_mb")
    if raw is None or str(raw).strip() == "":
        return 0
    try:
        return max(0, int(raw))
    except Exception:
        return 0


def _upgrade_disk_preflight_error(proc: dict[str, Any]) -> str:
    upgrade_cfg = proc.get("upgrade", {}) or {}
    required_mb = _min_free_disk_mb(upgrade_cfg)
    if required_mb <= 0:
        return ""
    free_bytes = current_disk_free_bytes()
    required_bytes = required_mb * _MB
    if free_bytes >= required_bytes:
        return ""
    free_mb = free_bytes // _MB
    return f"insufficient free disk for upgrade: free={free_mb}MB, required={required_mb}MB"


def _is_bootstrap_upgrade_runner() -> bool:
    return bool(str(os.environ.get("SANDBOX_GUARD_BOOTSTRAP_STARTED_AT_MS", "")).strip())


def schedule_upgrade(
    cfg_path: str,
    proc_name: str,
    target_version: str,
    *,
    independent_session: bool = False,
) -> subprocess.Popen[Any]:
    """异步拉起 upgrade-runner 子进程。"""
    script = Path(__file__).resolve().parent.parent.parent / "sandbox_guard.py"
    argv = [
        sys.executable,
        str(script),
        "upgrade-runner",
        "--config",
        cfg_path,
        "--process",
        proc_name,
        "--target-version",
        target_version,
    ]
    log("info", "upgrade.schedule", "准备触发升级任务", process=proc_name, target_version=target_version)
    if independent_session:
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=prepare_async_child_env(),
        )

    # legacy 与 daemon 调度路径保持原有 shell 子进程语义，避免影响存量镜像。
    cmd = (
        f"{shlex_quote(sys.executable)} {shlex_quote(str(script))} "
        f"upgrade-runner --config {shlex_quote(cfg_path)} "
        f"--process {shlex_quote(proc_name)} --target-version {shlex_quote(target_version)}"
    )
    return subprocess.Popen(cmd, shell=True, env=prepare_async_child_env())


def _upgrade_lock_file(cfg: dict[str, Any], proc_name: str) -> str:
    runtime = cfg.get("runtime", {}) or {}
    root_dir = Path(str(runtime.get("root_dir") or "/home/x/.daemon"))
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(proc_name or "unknown")).strip("_") or "unknown"
    return str(root_dir / "locks" / f"upgrade-{safe_name}.lock")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _load_version(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _save_version(path: Path, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(version.strip(), encoding="utf-8")


def _normalize_version(v: str) -> str:
    """归一化版本字符串，避免 BOM/空白导致误判。"""
    return str(v).replace("\ufeff", "").strip()


def _override_package_ref_version(package_ref: str, target_version: str) -> str:
    """用指定版本覆盖 package 引用的 tag。

    约定 package_ref 形如:
    - host/repo/name:1.2.3
    """
    ref = package_ref.strip()
    version = target_version.strip()
    if not ref or not version:
        return ref
    slash_pos = ref.rfind("/")
    colon_pos = ref.rfind(":")
    if colon_pos > slash_pos:
        return ref[: colon_pos + 1] + version
    return f"{ref}:{version}"


def _artifact_not_found(result: Any) -> bool:
    """判断 ORAS 失败是否明确表示目标 artifact 不存在。"""
    text = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}".lower()
    return any(
        marker in text
        for marker in (
            "404",
            "not found",
            "manifest unknown",
            "name unknown",
        )
    )


def _compare_numeric_versions(left: str, right: str) -> int | None:
    """比较纯数字点分版本；无法可靠排序时返回 None。"""
    pattern = re.compile(r"^[vV]?(\d+(?:\.\d+)*)$")
    left_match = pattern.fullmatch(_normalize_version(left))
    right_match = pattern.fullmatch(_normalize_version(right))
    if left_match is None or right_match is None:
        return None
    left_parts = [int(part) for part in left_match.group(1).split(".")]
    right_parts = [int(part) for part in right_match.group(1).split(".")]
    width = max(len(left_parts), len(right_parts))
    left_parts.extend([0] * (width - len(left_parts)))
    right_parts.extend([0] * (width - len(right_parts)))
    if left_parts < right_parts:
        return -1
    if left_parts > right_parts:
        return 1
    return 0


def _load_version_from_command(command: str) -> tuple[str, str]:
    """执行版本探测命令，返回 (version, error_message)。"""
    cmd = command.strip()
    if not cmd:
        return "", ""
    res = run_command(cmd, timeout=15)
    if res.returncode != 0:
        return "", (res.stderr.strip() or res.stdout.strip() or "version command failed")
    line = (res.stdout.strip().splitlines() or [""])[0].strip()
    if not line:
        return "", "version command output empty"
    return line, ""


def _stop_process(proc: dict[str, Any], cfg: dict[str, Any]) -> CommandResult:
    """按 manager 类型停止进程并返回结果，由调用方决定是否阻断升级。"""
    manager = str(proc.get("manager", "pm2"))
    if manager == "pm2":
        name = str((proc.get("manager_options", {}) or {}).get("pm2_name") or proc.get("name"))
        env = dict(os.environ)
        env["PM2_HOME"] = str((cfg.get("runtime", {}) or {}).get("pm2_home", "/home/x/.pm2"))
        try:
            return run_command(f"pm2 delete {shlex_quote(name)}", timeout=60, env=env)
        except subprocess.TimeoutExpired:
            log(
                "warn",
                "upgrade.process.stop_timeout",
                "升级前停止 PM2 进程超时，继续升级流程",
                process=str(proc.get("name", "")),
                pm2_name=name,
                returncode=124,
            )
            return CommandResult(124, "", "PM2 stop timeout")
    if manager == "supervisor":
        runtime_sup = (cfg.get("runtime", {}) or {}).get("supervisor", {}) or {}
        ctl_bin = str(runtime_sup["ctl_bin"])
        ctl_conf = str((proc.get("manager_options", {}) or {}).get("supervisor_conf") or runtime_sup.get("ctl_conf", ""))
        program = str((proc.get("manager_options", {}) or {}).get("supervisor_program") or proc.get("name"))
        base = f"{ctl_bin} -c {shlex_quote(ctl_conf)}" if ctl_conf else ctl_bin
        return run_command(f"{base} stop {shlex_quote(program)}", timeout=60)
    if manager == "direct":
        return get_strategy(manager).stop(proc, cfg)
    return CommandResult(2, "", f"unsupported manager: {manager}")


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


def _extract_or_copy_to_stage(package_path: Path, stage_dir: Path) -> tuple[bool, str]:
    lower = package_path.name.lower()
    if lower.endswith(".zip"):
        cmd = f"unzip -q -o {shlex_quote(str(package_path))} -d {shlex_quote(str(stage_dir))}"
        res = run_command(cmd, timeout=180)
        return (res.returncode == 0, res.stderr.strip() or res.stdout.strip())
    if lower.endswith(".tar.gz") or lower.endswith(".tgz"):
        cmd = f"tar -xzf {shlex_quote(str(package_path))} -C {shlex_quote(str(stage_dir))}"
        res = run_command(cmd, timeout=180)
        return (res.returncode == 0, res.stderr.strip() or res.stdout.strip())
    if lower.endswith(".tar"):
        cmd = f"tar -xf {shlex_quote(str(package_path))} -C {shlex_quote(str(stage_dir))}"
        res = run_command(cmd, timeout=180)
        return (res.returncode == 0, res.stderr.strip() or res.stdout.strip())
    # 非压缩包按单文件处理
    return True, ""


def _resolve_deploy_type(upgrade_cfg: dict[str, Any]) -> str:
    """解析部署形态。

    v1 兼容两类：
    - binary_file: 解压后替换单文件二进制
    - source_dir: 解压后替换整个源码目录
    """
    deploy_type = str(upgrade_cfg.get("deploy_type", "")).strip()
    if deploy_type:
        return deploy_type
    return "binary_file"


def _find_source_root(stage_dir: Path, source_root: str) -> Path | None:
    """在解压目录中查找源码根目录。"""
    root = str(source_root).strip()
    if root in {".", "./"}:
        return stage_dir if stage_dir.exists() and stage_dir.is_dir() else None

    direct = stage_dir / root
    if direct.exists() and direct.is_dir():
        return direct
    for candidate in stage_dir.rglob(root):
        if candidate.is_dir():
            return candidate
    return None


def _cleanup_upgrade_workdirs(stage_dir: Path, pkg_dir: Path) -> None:
    """清理单次升级产生的大文件目录，避免占用空间。"""
    if stage_dir.name.startswith(f"{_XAGENT_STAGE_PREFIX}-"):
        # xagent stage 可能包含大量小文件，交由启动指标落盘后的后台任务
        # 删除；这里只同步释放通常只有单个压缩包的 pkg 目录。
        try:
            _remove_path(pkg_dir)
        except Exception as exc:
            log(
                "warn",
                "workdir.cleanup.delete_failed",
                "升级 pkg 目录清理失败",
                path=str(pkg_dir),
                error=str(exc),
            )
        try:
            pkg_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            log(
                "warn",
                "workdir.cleanup.pkg_recreate_failed",
                "重建升级 pkg 目录失败",
                path=str(pkg_dir),
                error=str(exc),
            )
        return
    cleanup_current_upgrade_workdirs(stage_dir, pkg_dir)


def _remove_path(path: Path) -> None:
    """删除文件或目录。"""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _remove_path_quietly(path: Path) -> None:
    """静默删除文件或目录。

    仅用于清理历史遗留物，失败不影响主流程。
    """
    try:
        _remove_path(path)
    except Exception:
        pass


def _new_xagent_stage_dir(stage_parent: Path) -> Path:
    """为单次 xagent 升级分配不会与历史遗留目录冲突的 stage 路径。"""
    return stage_parent / f"{_XAGENT_STAGE_PREFIX}-{uuid.uuid4().hex}"


def _xagent_cleanup_paths(
    upgrade_cfg: dict[str, Any],
    *,
    runtime_root: str | Path | None = None,
    process: str = "xagent",
) -> list[Path]:
    """收集当前已存在的 xagent stage/installing/failed/previous 直接子项。"""
    patterns_by_parent: dict[Path, set[str]] = {}

    work_dir_path = str(upgrade_cfg.get("work_dir", "")).strip()
    if not work_dir_path and runtime_root is not None:
        work_dir_path = str(Path(runtime_root) / "work" / process)
    if work_dir_path:
        patterns_by_parent.setdefault(Path(work_dir_path), set()).update(
            {
                _XAGENT_STAGE_PREFIX,
                f"{_XAGENT_STAGE_PREFIX}-",
            }
        )

    source_deploy_dir = str(upgrade_cfg.get("source_deploy_dir", "")).strip()
    if source_deploy_dir:
        source_target = Path(source_deploy_dir)
        patterns_by_parent.setdefault(source_target.parent, set()).update(
            {
                # 兼容 1.0.15-1.0.20 在目标 PVC 下生成的 stage。
                _XAGENT_STAGE_PREFIX,
                f"{_XAGENT_STAGE_PREFIX}-",
                f".{source_target.name}.installing",
                f".{source_target.name}.installing-",
                f".{source_target.name}.failed",
                f".{source_target.name}.failed-",
                f".{source_target.name}.previous",
                f".{source_target.name}.previous-",
            }
        )

    binary_target_path = str(upgrade_cfg.get("binary_target", "")).strip()
    if binary_target_path:
        binary_target = Path(binary_target_path)
        patterns_by_parent.setdefault(binary_target.parent, set()).update(
            {
                f".{binary_target.name}.installing",
                f".{binary_target.name}.installing-",
                f".{binary_target.name}.failed",
                f".{binary_target.name}.failed-",
                f".{binary_target.name}.previous",
                f".{binary_target.name}.previous-",
            }
        )

    matched: set[Path] = set()
    for parent, patterns in patterns_by_parent.items():
        try:
            children = list(parent.iterdir())
        except Exception:
            continue
        for child in children:
            if any(child.name == pattern or (pattern.endswith("-") and child.name.startswith(pattern)) for pattern in patterns):
                matched.add(child)
    return sorted(matched, key=str)


def _schedule_xagent_cleanup(paths: list[Path], *, process: str) -> None:
    """后台删除已固定的 stage/installing/failed/previous 路径，不等待删除结果。"""
    if not paths:
        return
    try:
        subprocess.Popen(
            ["/bin/rm", "-rf", "--", *(str(path) for path in paths)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        log(
            "info",
            "upgrade.xagent.cleanup_scheduled",
            "已触发 xagent 历史 stage/installing/failed/previous 后台清理",
            process=process,
            count=len(paths),
        )
    except Exception as exc:
        log(
            "warn",
            "upgrade.xagent.cleanup_schedule_failed",
            "xagent 历史 stage/installing/failed/previous 后台清理触发失败，不影响升级结果",
            process=process,
            error=str(exc),
        )


def _prepare_meta_deploy_path(source: Path, target: Path, *, process: str) -> tuple[Path, Path, str]:
    """Prepare a complete sibling path while the old process is still running.

    A same-filesystem rename is O(1). ``copytree`` is retained only as an
    EXDEV fallback for deployments whose work and install directories live on
    different filesystems.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    installing = target.with_name(f".{target.name}.installing")
    previous = target.with_name(f".{target.name}.previous")
    _remove_path_quietly(installing)
    _remove_path_quietly(previous)
    try:
        source.rename(installing)
        mode = "rename"
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        mode = "cross_device_copy"
        try:
            if source.is_dir() and not source.is_symlink():
                shutil.copytree(source, installing, symlinks=True)
            else:
                shutil.copy2(source, installing, follow_symlinks=False)
        except Exception:
            _remove_path_quietly(installing)
            raise
    log(
        "info",
        "upgrade.meta.deploy_prepared",
        "升级文件已准备到安装目录同级位置",
        process=process,
        source=str(source),
        prepared=str(installing),
        mode=mode,
    )
    return installing, previous, mode


def _activate_meta_deploy_path(installing: Path, target: Path, previous: Path) -> bool:
    """Switch a prepared path into place and retain the old path for rollback."""
    had_previous = target.exists() or target.is_symlink()
    if had_previous:
        target.rename(previous)
    try:
        installing.rename(target)
    except Exception:
        if had_previous and previous.exists() and not target.exists():
            previous.rename(target)
        raise
    return had_previous


def _rollback_meta_deploy_path(target: Path, previous: Path, had_previous: bool) -> None:
    """Restore the pre-upgrade path after the new process fails to start."""
    _remove_path_quietly(target)
    if had_previous and previous.exists():
        previous.rename(target)


def _xagent_target_for_mode(upgrade_cfg: dict[str, Any], mode: str) -> Path:
    if mode == "source":
        return Path(str(upgrade_cfg["source_deploy_dir"]))
    return Path(str(upgrade_cfg["binary_target"]))


def _prepare_xagent_deploy_path(source: Path, target: Path, *, process: str) -> Path:
    """在旧进程运行期间，将本地解压结果准备到目标文件系统。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    installing = target.with_name(f".{target.name}.installing-{uuid.uuid4().hex}")
    mode = "rename"
    try:
        source.rename(installing)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        mode = "cross_device_copy"
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, installing, symlinks=True)
        else:
            shutil.copy2(source, installing, follow_symlinks=False)
    log(
        "info",
        "upgrade.xagent.deploy_prepared",
        "xagent 安装文件已准备到目标文件系统",
        process=process,
        source=str(source),
        prepared=str(installing),
        mode=mode,
    )
    return installing


def _activate_xagent_deploy_path(
    staged: Path,
    target: Path,
    old_target: Path | None,
    *,
    process: str,
) -> tuple[Path | None, bool]:
    """Switch a target-filesystem staging path into place using rename only."""
    target.parent.mkdir(parents=True, exist_ok=True)
    previous: Path | None = None
    had_previous = bool(old_target and (old_target.exists() or old_target.is_symlink()))
    if old_target is not None:
        previous = old_target.with_name(f".{old_target.name}.previous-{uuid.uuid4().hex}")

    # A stale target from the inactive package mode is not the running version.
    if old_target is not None and old_target != target:
        _remove_path(target)

    if had_previous and old_target is not None and previous is not None:
        old_target.rename(previous)
    try:
        staged.rename(target)
    except Exception:
        if had_previous and old_target is not None and previous is not None and previous.exists():
            previous.rename(old_target)
        raise

    log(
        "info",
        "upgrade.xagent.deploy_activated",
        "xagent 安装文件已在目标文件系统内原子切换",
        process=process,
        source=str(staged),
        target=str(target),
        previous=str(previous) if had_previous and previous is not None else "",
        mode="rename",
    )
    return previous, had_previous


def _rollback_xagent_deploy_path(
    target: Path,
    old_target: Path | None,
    previous: Path | None,
    had_previous: bool,
    *,
    process: str,
) -> None:
    failed: Path | None = None
    if target.exists() or target.is_symlink():
        failed = target.with_name(f".{target.name}.failed-{uuid.uuid4().hex}")
        try:
            target.rename(failed)
        except Exception:
            # 同目录 rename 理论上应为 O(1)；若文件系统异常导致失败，为确保
            # previous 能恢复到正式路径，保留同步删除作为最后兜底。
            _remove_path(target)
            failed = None
    if had_previous and old_target is not None and previous is not None and previous.exists():
        previous.rename(old_target)
    log(
        "warn",
        "upgrade.xagent.deploy_rollback",
        "xagent 新版本启动失败，已恢复旧安装目录",
        process=process,
        target=str(target),
        restored=str(old_target) if had_previous and old_target is not None else "",
        failed_path=str(failed) if failed is not None else "",
    )


def _xagent_pm2_name(up: dict[str, Any], mode: str) -> str:
    if mode == "source":
        return str(up.get("source_pm2_name", "xagent-dev"))
    return str(up.get("binary_pm2_name", "xagent"))


def _xagent_start_command(up: dict[str, Any], mode: str) -> str:
    if mode == "source":
        return str(up.get("source_start_command", "")).strip()
    return str(up.get("binary_start_command", "")).strip()


def _xagent_selected_proc(proc: dict[str, Any], mode: str) -> dict[str, Any]:
    selected = dict(proc)
    manager_options = dict((proc.get("manager_options", {}) or {}))
    manager_options["pm2_name"] = _xagent_pm2_name(proc.get("upgrade", {}) or {}, mode)
    selected["manager_options"] = manager_options
    selected["start_command"] = _xagent_start_command(proc.get("upgrade", {}) or {}, mode)
    return selected


def _xagent_start_existing(
    proc: dict[str, Any],
    cfg: dict[str, Any],
    *,
    mode: str,
    target_version: str,
    reason: str,
) -> tuple[bool, str]:
    name = str(proc.get("name", "xagent"))
    write_xagent_status(cfg, XAGENT_STATUS_STARTING, target_version=target_version)
    selected_proc = _xagent_selected_proc(proc, mode)
    strategy = get_strategy(str(proc.get("manager", "pm2")))
    pm2_name = str((selected_proc.get("manager_options", {}) or {}).get("pm2_name") or "")
    has_pid, probe_status, probe_detail = _pm2_pid_probe(pm2_name, cfg)
    if not has_pid and probe_status == "error":
        log(
            "warn",
            "upgrade.xagent.probe_degraded",
            "xagent probe 失败，按未运行处理并继续启动",
            process=name,
            mode=mode,
            reason=reason,
            error=probe_detail,
        )
    elif not has_pid:
        log(
            "info",
            "upgrade.xagent.probe_miss",
            "xagent probe 未识别到已运行进程，继续启动",
            process=name,
            mode=mode,
            reason=reason,
            detail=probe_detail,
        )
    if has_pid:
        return True, ""

    pre_ok, pre_err = _run_pre_recover_command(proc)
    if not pre_ok:
        return False, pre_err
    log("info", "upgrade.xagent.start_process", "确保 xagent 已启动", process=name, mode=mode, reason=reason)
    if hasattr(strategy, "start_xagent"):
        post = strategy.start_xagent(
            selected_proc,
            cfg,
            timeout_seconds=PM2_SKIP_LATEST_START_TIMEOUT_SECONDS,
            cleanup_before_start=False,
        )
    else:
        post = strategy.start_with_timeout(
            selected_proc,
            cfg,
            PM2_SKIP_LATEST_START_TIMEOUT_SECONDS,
        )
    if post.returncode != 0:
        return False, post.stderr.strip() or post.stdout.strip() or "start failed"
    return True, ""


def _xagent_installed_mode(proc: dict[str, Any]) -> str | None:
    """根据当前磁盘内容判断 xagent 已安装形态。"""
    return detect_installed_xagent_mode(proc.get("upgrade", {}) or {})


def _xagent_stop_existing(proc: dict[str, Any], cfg: dict[str, Any]) -> None:
    """仅在探测到旧 xagent PM2 进程存在时执行 delete。"""
    old_mode = _xagent_installed_mode(proc)
    if old_mode is None:
        log(
            "info",
            "upgrade.xagent.stop_skip_no_install",
            "未发现旧 xagent 安装形态，跳过 PM2 清理",
            process=str(proc.get("name", "xagent")),
        )
        return

    up = proc.get("upgrade", {}) or {}
    env = dict(os.environ)
    env["PM2_HOME"] = str((cfg.get("runtime", {}) or {}).get("pm2_home", "/home/x/.pm2"))
    preferred = _xagent_pm2_name(up, old_mode)
    names: list[str] = []
    for name in [preferred, str(up.get("binary_pm2_name", "xagent")).strip(), str(up.get("source_pm2_name", "xagent-dev")).strip()]:
        if not name:
            continue
        if name in names:
            continue
        names.append(name)

    for name in names:
        has_pid, probe_status, probe_detail = _pm2_pid_probe(name, cfg)
        if not has_pid:
            if probe_status == "error":
                log(
                    "warn",
                    "upgrade.xagent.stop_probe_failed",
                    "xagent 升级前 PM2 pid 探测失败，跳过该名称清理",
                    process=str(proc.get("name", "xagent")),
                    pm2_name=name,
                    error=probe_detail,
                )
            else:
                log(
                    "info",
                    "upgrade.xagent.stop_probe_miss",
                    "xagent 升级前未探测到 PM2 进程，跳过该名称清理",
                    process=str(proc.get("name", "xagent")),
                    pm2_name=name,
                    detail=probe_detail,
                )
            continue
        try:
            res = run_command(f"pm2 delete {shlex_quote(name)}", timeout=30, env=env)
        except subprocess.TimeoutExpired:
            log(
                "warn",
                "upgrade.xagent.pm2_delete_failed",
                "xagent 升级前 PM2 清理超时，继续升级流程",
                process=str(proc.get("name", "xagent")),
                pm2_name=name,
                returncode=124,
                stdout="",
                stderr="pm2 delete timeout",
            )
            continue
        if res.returncode != 0 and not _pm2_delete_not_found(res):
            log(
                "warn",
                "upgrade.xagent.pm2_delete_failed",
                "xagent 升级前 PM2 清理失败，继续升级流程",
                process=str(proc.get("name", "xagent")),
                pm2_name=name,
                returncode=res.returncode,
                stdout=(res.stdout or "")[-500:],
                stderr=(res.stderr or "")[-500:],
            )


def _run_pre_recover_command(proc: dict[str, Any]) -> tuple[bool, str]:
    """在 upgrade-runner 内复用进程恢复前置命令。"""
    cmd = str(proc.get("pre_recover_command", "")).strip()
    if not cmd:
        return True, ""
    res = run_command(cmd, timeout=30)
    if res.returncode == 0:
        return True, ""
    return False, res.stderr.strip() or res.stdout.strip() or "pre_recover_command failed"


def _strip_ansi(value: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", value)


def _pm2_delete_not_found(result: CommandResult) -> bool:
    text = _strip_ansi(f"{result.stdout}\n{result.stderr}").lower()
    return any(marker in text for marker in ["not found", "does not exist", "doesn't exist", "process or namespace not found"])


def _pm2_pid_probe(pm2_name: str, cfg: dict[str, Any]) -> tuple[bool, str, str]:
    """在 skip_latest 场景下做单个 PM2 名称的轻量探测。

    返回：
    - (True, "found", "")：明确识别到已有 PM2 pid
    - (False, "miss", detail)：未识别到 pid，但属于正常未命中
    - (False, "error", detail)：探测异常，用于降级日志
    """
    name = str(pm2_name).strip()
    if not name:
        return False, "error", "pm2 name missing"

    env = dict(os.environ)
    env["PM2_HOME"] = str((cfg.get("runtime", {}) or {}).get("pm2_home", "/home/x/.pm2"))
    try:
        res = run_command(f"pm2 pid {shlex_quote(name)}", timeout=2, env=env)
    except subprocess.TimeoutExpired:
        return False, "error", f"{name}: timeout"
    if res.returncode != 0:
        detail = _strip_ansi(res.stderr.strip() or res.stdout.strip() or "pm2 pid failed")
        if any(marker in detail.lower() for marker in ["not found", "does not exist", "doesn't exist"]):
            return False, "miss", f"{name}: {detail}"
        return False, "error", f"{name}: {detail}"

    lines = [_strip_ansi(line.strip()) for line in (res.stdout or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if line.isdigit() and int(line) > 0:
            return True, "found", name
    return False, "miss", f"{name}: {lines[-1] if lines else 'empty output'}"


def _xagent_health_check_ready(proc: dict[str, Any], *, request_timeout_seconds: float) -> bool:
    health = proc.get("health_check")
    if not isinstance(health, dict):
        return False
    if str(health.get("type", "")).strip() != "http_json":
        return False

    url = str(health.get("url", "")).strip()
    expect_field = str(health.get("expect_json_field", "")).strip()
    expect_value = str(health.get("expect_json_value", "")).strip()
    if not url or not expect_field:
        return False

    timeout_text = f"{request_timeout_seconds:g}"
    command_timeout = max(1, int(request_timeout_seconds) + 1)
    res = run_command(f"curl -fsS --max-time {timeout_text} {shlex_quote(url)}", timeout=command_timeout)
    if res.returncode != 0:
        return False
    try:
        payload = json.loads(res.stdout.strip() or "{}")
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    return str(payload.get(expect_field)) == expect_value


def _log_xagent_init_ready_if_possible(
    cfg: dict[str, Any],
    proc: dict[str, Any],
    *,
    upgraded: bool,
    mode: str,
    target_version: str,
) -> int | None:
    ready_wait = ((proc.get("upgrade", {}) or {}).get("ready_wait", {}) or {})
    timeout_seconds = float(ready_wait.get("timeout_seconds", XAGENT_READY_WAIT_TIMEOUT_SECONDS))
    interval_seconds = float(ready_wait.get("interval_seconds", XAGENT_READY_WAIT_INTERVAL_SECONDS))
    request_timeout_seconds = float(
        ready_wait.get("request_timeout_seconds", XAGENT_READY_REQUEST_TIMEOUT_SECONDS)
    )
    deadline = time.monotonic() + timeout_seconds
    while True:
        if _xagent_health_check_ready(proc, request_timeout_seconds=request_timeout_seconds):
            ready_at_ms = int(time.time() * 1000)
            log(
                "info",
                "sandbox.init.ready",
                "沙箱准备就绪",
                process=str(proc.get("name", "xagent")),
                upgraded=upgraded,
                mode=mode,
                target_version=target_version,
            )
            clear_xagent_status(cfg)
            return ready_at_ms
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval_seconds, remaining))

    log(
        "warn",
        "sandbox.init.ready_wait_timeout",
        "xagent 启动已触发，但在等待窗口内未达到健康状态，跳过初始化时间统计",
        process=str(proc.get("name", "xagent")),
        upgraded=upgraded,
        mode=mode,
        target_version=target_version,
    )
    return None


def execute_meta_package_upgrade(proc: dict[str, Any], cfg: dict[str, Any], requested_target_version: str = "auto") -> tuple[bool, str, str, bool]:
    """
    固定策略：meta + package。

    返回：
    - ok
    - err
    - resolved_target_version
    - skipped（True 表示已是最新，无需升级）
    """
    up = proc.get("upgrade", {}) or {}

    deploy_type = _resolve_deploy_type(up)
    required = ["oras_bin", "oras_host", "oras_user", "oras_password", "meta_ref"]
    if deploy_type == "binary_file":
        required.append("binary_target")
    elif deploy_type == "source_dir":
        required.extend(["deploy_dir", "source_root"])
    else:
        return False, f"unsupported deploy_type: {deploy_type}", "", False
    for key in required:
        if not str(up.get(key, "")).strip():
            return False, f"upgrade.{key} 为空", "", False

    name = str(proc.get("name", "unknown"))
    log("info", "upgrade.meta.start", "开始执行升级流程", process=name)
    oras_bin = Path(str(up["oras_bin"]))
    if not oras_bin.exists():
        return False, f"oras binary missing: {oras_bin}", "", False

    runtime = cfg["runtime"]
    root_dir = Path(str(runtime["root_dir"]))
    work_dir = Path(str(up.get("work_dir", str(root_dir / "work" / name))))
    cleanup_upgrade_workdir(work_dir, min_age_seconds=0, process=name, reason="upgrade_preflight")
    meta_dir = work_dir / "meta"
    pkg_dir = work_dir / "pkg"
    stage_dir = work_dir / f"stage_{int(time.time())}"
    for d in [meta_dir, pkg_dir, stage_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # 1) 登录 ORAS
    login_cmd = (
        f"{shlex_quote(str(oras_bin))} login --plain-http {shlex_quote(str(up['oras_host']))} "
        f"-u {shlex_quote(str(up['oras_user']))} -p {shlex_quote(str(up['oras_password']))}"
    )
    log("info", "upgrade.meta.login", "开始 ORAS 登录", process=name, host=str(up["oras_host"]))
    login = run_command(login_cmd, timeout=60)
    if login.returncode != 0:
        return False, f"oras login failed: {login.stderr.strip() or login.stdout.strip()}", "", False

    # 2) 拉取 meta
    for old in meta_dir.glob("*"):
        if old.is_file():
            old.unlink()
    meta_ref = str(up["meta_ref"])
    log("info", "upgrade.meta.pull_meta", "开始拉取升级元数据", process=name, meta_ref=meta_ref)
    meta_pull = run_command(
        f"cd {shlex_quote(str(meta_dir))} && {shlex_quote(str(oras_bin))} pull --plain-http {shlex_quote(meta_ref)}",
        timeout=120,
    )
    if meta_pull.returncode != 0:
        return False, f"meta pull failed: {meta_pull.stderr.strip() or meta_pull.stdout.strip()}", "", False

    meta_file = meta_dir / str(up.get("meta_file", "release.json"))
    if not meta_file.exists():
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
    sha256_expect = str(release.get("sha256", "")).strip().lower()
    requested_version = _normalize_version(requested_target_version)
    if requested_version and requested_version != "auto":
        target_version = requested_version
        package_ref = _override_package_ref_version(package_ref, requested_version)
        sha256_expect = ""
        log("info", "upgrade.meta.force_version", "使用手动指定版本拉取升级包", process=name, target_version=target_version)
    if not target_version or not package_ref:
        return False, "release json missing required fields: version/package", "", False

    configured_version_file = str(up.get("current_version_file", "")).strip()
    current_version_file = Path(configured_version_file) if configured_version_file else None
    current_version = ""
    current_version_command = str(up.get("current_version_command", "")).strip()
    if current_version_command:
        current_version, probe_err = _load_version_from_command(current_version_command)
        if probe_err:
            log(
                "warn",
                "upgrade.version_probe.command_failed",
                "版本命令探测失败，回退到版本文件探测",
                process=name,
                error=probe_err,
            )
    if not current_version and current_version_file is not None:
        current_version = _load_version(current_version_file)
    current_version = _normalize_version(current_version)
    log(
        "info",
        "upgrade.meta.version_compare",
        "版本比较",
        process=name,
        current_version=current_version,
        target_version=target_version,
        version_file=str(current_version_file) if current_version_file else "",
    )
    if current_version and current_version == target_version:
        applicability = process_applicability(proc, cfg)
        if not applicability.applicable:
            log(
                "info",
                "upgrade.meta.skip_inapplicable_before_start",
                "进程已变为不适用，跳过最新版本启动确认",
                process=name,
                reason=applicability.reason,
            )
            _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
            return True, "", target_version, True
        log("info", "upgrade.meta.skip_latest", "当前已是最新版本，跳过升级", process=name, version=target_version)
        strategy = get_strategy(str(proc.get("manager", "pm2")))
        manager = str(proc.get("manager", "pm2"))
        should_start = False
        probe_status = ""
        probe_detail = ""
        if manager == "pm2":
            pm2_name = str((proc.get("manager_options", {}) or {}).get("pm2_name") or name)
            has_pid, probe_status, probe_detail = _pm2_pid_probe(pm2_name, cfg)
            should_start = not has_pid
            if should_start and probe_status == "error":
                log(
                    "warn",
                    "upgrade.meta.probe_degraded",
                    "PM2 pid 探测失败，按未运行处理并继续启动",
                    process=name,
                    pm2_name=pm2_name,
                    error=probe_detail,
                )
        else:
            probe = strategy.probe(proc, cfg)
            probe_status = str(probe.raw_status)
            probe_detail = probe.message
            should_start = not probe.exists or probe_status == "ERROR"
        if should_start:
            log(
                "info",
                "upgrade.meta.start_process",
                "当前已是最新版本，确保进程已启动",
                process=name,
                probe_status=probe_status,
                probe_message=probe_detail,
            )
            pre_ok, pre_err = _run_pre_recover_command(proc)
            if not pre_ok:
                _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
                return False, pre_err, target_version, True
            if hasattr(strategy, "start_with_timeout"):
                post = strategy.start_with_timeout(proc, cfg, PM2_SKIP_LATEST_START_TIMEOUT_SECONDS)
            else:
                post = strategy.start(proc, cfg)
            if post.returncode != 0:
                _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
                return False, post.stderr.strip() or post.stdout.strip() or "start failed", target_version, True
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return True, "", target_version, True

    disk_err = _upgrade_disk_preflight_error(proc)
    if disk_err:
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return False, disk_err, target_version, False

    # 3) 拉取 package
    for old in pkg_dir.glob("*"):
        if old.is_file():
            old.unlink()
    log("info", "upgrade.meta.pull_package", "开始拉取升级包", process=name, package_ref=package_ref, target_version=target_version)
    pkg_pull = run_command(
        f"cd {shlex_quote(str(pkg_dir))} && {shlex_quote(str(oras_bin))} pull --plain-http {shlex_quote(package_ref)}",
        timeout=180,
    )
    if pkg_pull.returncode != 0:
        return False, f"package pull failed: {pkg_pull.stderr.strip() or pkg_pull.stdout.strip()}", target_version, False

    package_path = _pick_package_file(pkg_dir, package_file_name)
    if package_path is None:
        return False, f"package file missing in {pkg_dir}", target_version, False
    log("info", "upgrade.meta.package_ready", "升级包已就绪", process=name, package=str(package_path))

    # 4) sha256 可选校验
    if sha256_expect:
        actual = _sha256_file(package_path).lower()
        if actual != sha256_expect:
            return False, f"sha256 mismatch: expect={sha256_expect} actual={actual}", target_version, False
    else:
        log("warn", "upgrade.meta.sha256.skip", "release 未提供 sha256，跳过校验", process=name)

    # 5) 解压到 stage / 或直接复制
    ok, err = _extract_or_copy_to_stage(package_path, stage_dir)
    if not ok:
        return False, f"extract failed: {err}", target_version, False

    applicability = process_applicability(proc, cfg)
    if not applicability.applicable:
        log(
            "info",
            "upgrade.meta.skip_inapplicable_before_deploy",
            "升级文件替换前进程已变为不适用，取消本次部署",
            process=name,
            reason=applicability.reason,
        )
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return True, "", target_version, True

    # 6) 在旧进程仍运行时准备完整安装路径，避免复制时间进入停机窗口。
    try:
        if deploy_type == "binary_file":
            binary_name = str(up.get("binary_name", "XAgent"))
            staged_candidates = [p for p in stage_dir.rglob(binary_name) if p.is_file()]
            package_name_lower = package_path.name.lower()
            package_is_archive = package_name_lower.endswith((".zip", ".tar.gz", ".tgz", ".tar"))
            if not staged_candidates and package_path.is_file() and not package_is_archive:
                # 非压缩包本身就是待部署的单文件，直接进入后续 rename 流程。
                staged_candidates = [package_path]
            if not staged_candidates:
                _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
                return False, f"binary missing in stage: {binary_name}", target_version, False

            staged_deploy_path = staged_candidates[0]
            log("info", "upgrade.meta.binary_ready", "升级二进制准备完成", process=name, binary=str(staged_deploy_path))
            if staged_deploy_path.stat().st_size <= 0:
                _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
                return False, f"binary empty: {staged_deploy_path}", target_version, False
            staged_deploy_path.chmod(staged_deploy_path.stat().st_mode | 0o111)
            target_deploy_path = Path(str(up["binary_target"]))
        else:
            source_root = str(up["source_root"])
            staged_source_dir = _find_source_root(stage_dir, source_root)
            if staged_source_dir is None:
                _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
                return False, f"source root missing in stage: {source_root}", target_version, False
            log("info", "upgrade.meta.source_ready", "升级源码目录准备完成", process=name, source_dir=str(staged_source_dir))
            staged_deploy_path = staged_source_dir
            target_deploy_path = Path(str(up["deploy_dir"]))

        installing_path, previous_path, deploy_mode = _prepare_meta_deploy_path(
            staged_deploy_path,
            target_deploy_path,
            process=name,
        )
    except Exception as exc:
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return False, f"prepare deploy failed: {exc}", target_version, False

    # 7) 停进程 -> rename 切换 -> 启动
    log("info", "upgrade.meta.stop_process", "开始停止旧进程", process=name)
    stop_result = _stop_process(proc, cfg)
    if str(proc.get("manager", "pm2")).strip() == "direct" and stop_result.returncode != 0:
        _remove_path_quietly(installing_path)
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return False, stop_result.stderr.strip() or stop_result.stdout.strip() or "stop failed", target_version, False

    try:
        had_previous = _activate_meta_deploy_path(installing_path, target_deploy_path, previous_path)
        if deploy_type == "binary_file":
            target_deploy_path.chmod(target_deploy_path.stat().st_mode | 0o111)
        log(
            "info",
            "upgrade.meta.deploy_activated",
            "升级文件已切换到运行位置",
            process=name,
            target=str(target_deploy_path),
            mode=deploy_mode,
        )
    except Exception as exc:
        _remove_path_quietly(installing_path)
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return False, f"replace failed: {exc}", target_version, False

    strategy = get_strategy(str(proc.get("manager", "pm2")))
    log("info", "upgrade.meta.start_process", "开始启动新进程", process=name)
    post = strategy.start(proc, cfg)
    if post.returncode != 0:
        try:
            _rollback_meta_deploy_path(target_deploy_path, previous_path, had_previous)
            if had_previous:
                rollback_start = strategy.start(proc, cfg)
                if rollback_start.returncode != 0:
                    log(
                        "warn",
                        "upgrade.meta.rollback_start_failed",
                        "旧版本目录已恢复，但进程重新启动失败",
                        process=name,
                        error=rollback_start.stderr.strip() or rollback_start.stdout.strip(),
                    )
        except Exception as rollback_exc:
            log(
                "error",
                "upgrade.meta.rollback_failed",
                "新版本启动失败且旧版本目录恢复失败",
                process=name,
                error=str(rollback_exc),
            )
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return False, post.stderr.strip() or post.stdout.strip() or "start failed", target_version, False

    _remove_path_quietly(previous_path)

    if current_version_file is not None:
        try:
            _save_version(current_version_file, target_version)
            # 写后读一次，确认最终落盘内容符合预期。
            if _load_version(current_version_file) != target_version:
                log(
                    "warn",
                    "upgrade.version_file.verify_failed",
                    "版本文件写入后校验不一致，下次可能重复升级",
                    process=name,
                    path=str(current_version_file),
                    expected=target_version,
                )
        except Exception as exc:
            log(
                "warn",
                "upgrade.version_file.write_failed",
                "版本文件写入失败，下次可能重复升级",
                process=name,
                path=str(current_version_file),
                error=str(exc),
            )
    _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
    return True, "", target_version, False


def execute_xagent_package_upgrade(proc: dict[str, Any], cfg: dict[str, Any], requested_target_version: str = "auto") -> tuple[bool, str, str, bool]:
    """xagent 专用升级策略。

    支持两种包形态：
    - 二进制包：解压后包含 `XAgent`
    - 源码包：解压后包含 `LD96.03_XAgent/`
    """
    up = proc.get("upgrade", {}) or {}
    required = [
        "oras_bin",
        "oras_host",
        "oras_user",
        "oras_password",
        "meta_ref",
        "binary_target",
        "source_root",
        "source_deploy_dir",
        "binary_start_command",
        "source_start_command",
    ]
    for key in required:
        if not str(up.get(key, "")).strip():
            return False, f"upgrade.{key} 为空", "", False

    name = str(proc.get("name", "xagent"))
    startup_timing: dict[str, Any] = {}
    log("info", "upgrade.xagent.start", "开始执行 xagent 升级流程", process=name)
    write_xagent_status(cfg, XAGENT_STATUS_CHECKING_UPDATE, target_version=requested_target_version)
    oras_bin = Path(str(up["oras_bin"]))
    if not oras_bin.exists():
        return False, f"oras binary missing: {oras_bin}", "", False

    runtime = cfg["runtime"]
    root_dir = Path(str(runtime["root_dir"]))
    work_dir = Path(str(up.get("work_dir", str(root_dir / "work" / name))))
    cleanup_upgrade_workdir(work_dir, min_age_seconds=0, process=name, reason="upgrade_preflight")
    meta_dir = work_dir / "meta"
    pkg_dir = work_dir / "pkg"
    stage_dir = _new_xagent_stage_dir(work_dir)
    for d in [meta_dir, pkg_dir]:
        d.mkdir(parents=True, exist_ok=True)

    login_cmd = (
        f"{shlex_quote(str(oras_bin))} login --plain-http {shlex_quote(str(up['oras_host']))} "
        f"-u {shlex_quote(str(up['oras_user']))} -p {shlex_quote(str(up['oras_password']))}"
    )
    log("info", "upgrade.xagent.login", "开始 ORAS 登录", process=name, host=str(up["oras_host"]))
    login = run_command(login_cmd, timeout=60)
    if login.returncode != 0:
        return False, f"oras login failed: {login.stderr.strip() or login.stdout.strip()}", "", False

    for old in meta_dir.glob("*"):
        if old.is_file():
            old.unlink()
    requested_version = _normalize_version(requested_target_version) or "auto"
    requested_mode = requested_version.lower()
    is_auto = requested_mode == "auto"
    is_stable = requested_mode == "stable"
    is_exact = not is_auto and not is_stable
    configured_meta_ref = str(up["meta_ref"])
    meta_ref = _override_package_ref_version(configured_meta_ref, requested_version) if is_exact else configured_meta_ref
    log("info", "upgrade.xagent.pull_meta", "开始拉取升级元数据", process=name, meta_ref=meta_ref)
    meta_pull = run_command(
        f"cd {shlex_quote(str(meta_dir))} && {shlex_quote(str(oras_bin))} pull --plain-http {shlex_quote(meta_ref)}",
        timeout=120,
    )
    release: dict[str, Any] | None = None
    legacy_meta_fallback = False
    if meta_pull.returncode != 0:
        if not is_exact or not _artifact_not_found(meta_pull):
            return False, f"meta pull failed: {meta_pull.stderr.strip() or meta_pull.stdout.strip()}", "", False
        package_repository = str(up.get("package_repository", "")).strip().rstrip(":")
        package_file_name = str(up.get("package_file", "")).strip()
        if package_repository and package_file_name:
            release = {
                "version": requested_version,
                "package": f"{package_repository}:{requested_version}",
                "file": package_file_name,
                "sha256": "",
            }
            log(
                "warn",
                "upgrade.xagent.version_meta_missing",
                "指定版本元数据不存在，按配置直接拉取版本包并跳过 checksum",
                process=name,
                target_version=requested_version,
                meta_ref=meta_ref,
            )
        else:
            legacy_meta_fallback = True
            for old in meta_dir.glob("*"):
                if old.is_file():
                    old.unlink()
            log(
                "warn",
                "upgrade.xagent.version_meta_legacy_fallback",
                "指定版本元数据不存在且未配置包仓库，回退 latest 元数据兼容旧配置",
                process=name,
                target_version=requested_version,
            )
            meta_pull = run_command(
                f"cd {shlex_quote(str(meta_dir))} && {shlex_quote(str(oras_bin))} pull --plain-http {shlex_quote(configured_meta_ref)}",
                timeout=120,
            )
            if meta_pull.returncode != 0:
                return False, f"meta pull failed: {meta_pull.stderr.strip() or meta_pull.stdout.strip()}", "", False

    if release is None:
        meta_file = meta_dir / str(up.get("meta_file", "release.json"))
        if not meta_file.exists():
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
    sha256_expect = str(release.get("sha256", "")).strip().lower()
    if is_exact and not legacy_meta_fallback and target_version != requested_version:
        return False, f"release json version mismatch: expect={requested_version} actual={target_version}", "", False
    if is_exact and legacy_meta_fallback:
        target_version = requested_version
        package_ref = _override_package_ref_version(package_ref, requested_version)
        sha256_expect = ""
    if is_exact:
        log(
            "info",
            "upgrade.xagent.force_version",
            "使用手动指定版本拉取升级包",
            process=name,
            target_version=target_version,
            checksum_enabled=bool(sha256_expect),
        )
    elif is_stable:
        log("info", "upgrade.xagent.force_stable", "强制收敛到当前稳定版本", process=name, target_version=target_version)
    if not target_version or not package_ref:
        return False, "release json missing required fields: version/package", "", False

    configured_version_file = str(up.get("current_version_file", "")).strip()
    current_version_file = Path(configured_version_file) if configured_version_file else None
    current_version = ""
    current_version_command = str(up.get("current_version_command", "")).strip()
    if current_version_command:
        current_version, probe_err = _load_version_from_command(current_version_command)
        if probe_err:
            log(
                "warn",
                "upgrade.version_probe.command_failed",
                "版本命令探测失败，回退到版本文件探测",
                process=name,
                error=probe_err,
            )
    if not current_version and current_version_file is not None:
        current_version = _load_version(current_version_file)
    current_version = _normalize_version(current_version)
    log(
        "info",
        "upgrade.xagent.version_compare",
        "版本比较",
        process=name,
        current_version=current_version,
        target_version=target_version,
        version_file=str(current_version_file) if current_version_file else "",
    )
    check_finished_at_ms = _now_ms()
    skip_reason = ""
    if current_version and current_version == target_version:
        skip_reason = "same_version"
    elif is_auto and current_version:
        version_order = _compare_numeric_versions(target_version, current_version)
        if version_order is None:
            skip_reason = "version_order_unknown"
            log(
                "warn",
                "upgrade.xagent.auto_skip_unordered_version",
                "版本格式无法可靠排序，auto 模式为避免降级而跳过更新",
                process=name,
                current_version=current_version,
                target_version=target_version,
            )
        elif version_order < 0:
            skip_reason = "target_older"
            log(
                "info",
                "upgrade.xagent.auto_skip_downgrade",
                "auto 模式禁止自动降级，保留当前 xagent 版本",
                process=name,
                current_version=current_version,
                target_version=target_version,
            )
    if skip_reason:
        startup_timing.update(
            {
                "xagentUpdateCheckFinishedAtMs": check_finished_at_ms,
                "xagentNeedUpdate": False,
                "xagentStartStartedAtMs": check_finished_at_ms,
            }
        )
        if skip_reason == "same_version":
            log("info", "upgrade.xagent.skip_latest", "当前已是目标版本，跳过升级", process=name, version=target_version)
        mode = _xagent_installed_mode(proc)
        if mode:
            start_ok, start_err = _xagent_start_existing(
                proc,
                cfg,
                mode=mode,
                target_version=current_version,
                reason="skip_latest",
            )
            if not start_ok:
                _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
                return False, start_err, current_version, True
        merge_xagent_startup_timing(cfg, startup_timing)
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return True, "", current_version, True

    disk_err = _upgrade_disk_preflight_error(proc)
    if disk_err:
        if _is_bootstrap_upgrade_runner():
            mode = _xagent_installed_mode(proc)
            if mode:
                startup_timing.update(
                    {
                        "xagentUpdateCheckFinishedAtMs": check_finished_at_ms,
                        "xagentNeedUpdate": False,
                        "xagentStartStartedAtMs": check_finished_at_ms,
                    }
                )
                failed_at_ms = _now_ms()
                _append_upgrade_failed_report(cfg, name, target_version, disk_err, failed_at_ms)
                log(
                    "warn",
                    "upgrade.xagent.disk_preflight_bootstrap_fallback",
                    "启动链路磁盘空间不足，跳过更新并尝试启动本地 xagent",
                    process=name,
                    current_version=current_version,
                    target_version=target_version,
                    error=disk_err,
                )
                start_ok, start_err = _xagent_start_existing(
                    proc,
                    cfg,
                    mode=mode,
                    target_version=current_version or target_version,
                    reason="disk_preflight_bootstrap_fallback",
                )
                if start_ok:
                    merge_xagent_startup_timing(cfg, startup_timing)
                    _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
                    return True, "", current_version or target_version, True
                _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
                return False, f"{disk_err}; fallback start failed: {start_err}", current_version or target_version, True
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return False, disk_err, target_version, False

    for old in pkg_dir.glob("*"):
        if old.is_file():
            old.unlink()
    startup_timing.update(
        {
            "xagentUpdateCheckFinishedAtMs": check_finished_at_ms,
            "xagentNeedUpdate": True,
            "xagentDownloadStartedAtMs": check_finished_at_ms,
        }
    )
    write_xagent_status(cfg, XAGENT_STATUS_UPGRADING, target_version=target_version)
    log("info", "upgrade.xagent.pull_package", "开始拉取升级包", process=name, package_ref=package_ref, target_version=target_version)
    pkg_pull = run_command(
        f"cd {shlex_quote(str(pkg_dir))} && {shlex_quote(str(oras_bin))} pull --plain-http {shlex_quote(package_ref)}",
        timeout=180,
    )
    if pkg_pull.returncode != 0:
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return False, f"package pull failed: {pkg_pull.stderr.strip() or pkg_pull.stdout.strip()}", target_version, False

    package_path = _pick_package_file(pkg_dir, package_file_name)
    if package_path is None:
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return False, f"package file missing in {pkg_dir}", target_version, False
    log("info", "upgrade.xagent.package_ready", "升级包已就绪", process=name, package=str(package_path))

    if sha256_expect:
        actual = _sha256_file(package_path).lower()
        if actual != sha256_expect:
            _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
            return False, f"sha256 mismatch: expect={sha256_expect} actual={actual}", target_version, False
    else:
        log("warn", "upgrade.xagent.sha256.skip", "release 未提供 sha256，跳过校验", process=name)

    package_verified_at_ms = _now_ms()
    startup_timing.update(
        {
            "xagentDownloadFinishedAtMs": package_verified_at_ms,
            "xagentInstallStartedAtMs": package_verified_at_ms,
        }
    )
    try:
        stage_dir.mkdir(parents=True, exist_ok=False)
    except Exception as exc:
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return False, f"prepare local staging failed: {exc}", target_version, False
    extract_started_at = time.monotonic()
    ok, err = _extract_or_copy_to_stage(package_path, stage_dir)
    if not ok:
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return False, f"extract failed: {err}", target_version, False
    log(
        "info",
        "upgrade.xagent.extract_complete",
        "xagent 升级包解压完成",
        process=name,
        duration_ms=round((time.monotonic() - extract_started_at) * 1000),
    )

    source_root = str(up["source_root"]).strip()
    binary_name = str(up.get("binary_name", "XAgent"))
    mode, staged_payload = detect_staged_xagent_package(
        stage_dir,
        package_path,
        source_root=source_root,
        binary_name=binary_name,
    )

    if mode == "source" and staged_payload is not None:
        log("info", "upgrade.xagent.mode.source", "识别到源码包形态", process=name, source_dir=str(staged_payload))
    elif mode == "binary" and staged_payload is not None:
        log("info", "upgrade.xagent.mode.binary", "识别到二进制包形态", process=name, binary=str(staged_payload))
    else:
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return False, f"xagent package shape unsupported: source_root={source_root}, binary_name={binary_name}", target_version, False

    if staged_payload == package_path:
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return False, "raw xagent binary package unsupported; zip package required", target_version, False

    old_mode = _xagent_installed_mode(proc)
    old_target = _xagent_target_for_mode(up, old_mode) if old_mode else None
    new_target = _xagent_target_for_mode(up, mode)
    if mode == "binary":
        staged_payload.chmod(staged_payload.stat().st_mode | 0o111)
    try:
        prepared_payload = _prepare_xagent_deploy_path(
            staged_payload,
            new_target,
            process=name,
        )
    except Exception as exc:
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return False, f"prepare deploy failed: {exc}", target_version, False

    write_xagent_status(cfg, XAGENT_STATUS_STARTING, target_version=target_version)
    log("info", "upgrade.xagent.stop_process", "开始停止 xagent 旧进程", process=name)
    _xagent_stop_existing(proc, cfg)

    strategy = get_strategy(str(proc.get("manager", "pm2")))
    previous: Path | None = None
    had_previous = False
    activated = False
    try:
        previous, had_previous = _activate_xagent_deploy_path(
            prepared_payload,
            new_target,
            old_target,
            process=name,
        )
        activated = True
        if mode == "binary":
            new_target.chmod(new_target.stat().st_mode | 0o111)

        startup_timing["xagentInstallFinishedAtMs"] = _now_ms()
        startup_timing["xagentStartStartedAtMs"] = startup_timing["xagentInstallFinishedAtMs"]
        selected_proc = _xagent_selected_proc(proc, mode)
        pre_ok, pre_err = _run_pre_recover_command(proc)
        if not pre_ok:
            raise RuntimeError(pre_err)
        log("info", "upgrade.xagent.start_process", "开始启动 xagent 新进程", process=name, mode=mode)
        if hasattr(strategy, "start_xagent"):
            post = strategy.start_xagent(selected_proc, cfg, cleanup_before_start=False)
        else:
            post = strategy.start(selected_proc, cfg)
        if post.returncode != 0:
            raise RuntimeError(post.stderr.strip() or post.stdout.strip() or "start failed")
    except Exception as exc:
        if activated:
            try:
                _rollback_xagent_deploy_path(
                    new_target,
                    old_target,
                    previous,
                    had_previous,
                    process=name,
                )
            except Exception as rollback_exc:
                log(
                    "error",
                    "upgrade.xagent.deploy_rollback_failed",
                    "xagent 新版本启动失败且旧安装目录恢复失败",
                    process=name,
                    error=str(rollback_exc),
                )
        if old_mode:
            try:
                old_proc = _xagent_selected_proc(proc, old_mode)
                if hasattr(strategy, "start_xagent"):
                    rollback_start = strategy.start_xagent(old_proc, cfg, cleanup_before_start=False)
                else:
                    rollback_start = strategy.start(old_proc, cfg)
                if rollback_start.returncode != 0:
                    log(
                        "warn",
                        "upgrade.xagent.rollback_start_failed",
                        "旧 xagent 安装已恢复，但进程重新启动失败",
                        process=name,
                        mode=old_mode,
                        error=rollback_start.stderr.strip() or rollback_start.stdout.strip(),
                    )
            except Exception as rollback_start_exc:
                log(
                    "warn",
                    "upgrade.xagent.rollback_start_failed",
                    "旧 xagent 安装已恢复，但进程重新启动异常",
                    process=name,
                    mode=old_mode,
                    error=str(rollback_start_exc),
                )
        _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
        return False, str(exc), target_version, False

    alternate_target = _xagent_target_for_mode(up, "binary" if mode == "source" else "source")
    _remove_path_quietly(alternate_target)

    if current_version_file is not None:
        try:
            _save_version(current_version_file, target_version)
        except Exception as exc:
            log(
                "warn",
                "upgrade.version_file.write_failed",
                "版本文件写入失败，下次可能重复升级",
                process=name,
                path=str(current_version_file),
                error=str(exc),
            )
    merge_xagent_startup_timing(cfg, startup_timing)
    _cleanup_upgrade_workdirs(stage_dir, pkg_dir)
    return True, "", target_version, False


def run_upgrade_runner(cfg: dict[str, Any], proc_name: str, target_version: str) -> int:
    """upgrade-runner 主入口。"""
    log("info", "upgrade.runner.start", "upgrade-runner 启动", process=proc_name, target_version=target_version)
    runtime = cfg["runtime"]
    event_file = runtime["event_file"]
    proc = None
    for p in cfg.get("processes", []) or []:
        if p.get("name") == proc_name:
            proc = p
            break
    if not proc:
        log("error", "upgrade.runner.process_not_found", "升级失败：进程不存在", process=proc_name)
        failed_at_ms = _now_ms()
        _append_upgrade_failed_report(cfg, proc_name, target_version, "process not found", failed_at_ms)
        append_upgrade_event(
            event_file,
            {
                "process": proc_name,
                "requested_target_version": target_version,
                "ok": False,
                "error": "process not found",
                "finished_at": now_iso(),
                "task_id": str(uuid.uuid4()),
            },
        )
        return 2

    applicability = process_applicability(proc, cfg)
    if not applicability.applicable:
        log(
            "info",
            "upgrade.runner.skip_inapplicable",
            "当前沙箱不适用该进程，跳过升级",
            process=proc_name,
            sandbox_type=applicability.sandbox_type,
            sandbox_platform=applicability.sandbox_platform,
            reason=applicability.reason,
        )
        return 0

    up = proc.get("upgrade", {}) or {}
    strategy = str(up.get("strategy", "meta_package")).strip()
    if strategy not in {"meta_package", "xagent_package", "code_server_package"}:
        log(
            "error",
            "upgrade.runner.strategy_unsupported",
            "升级失败：不支持的升级策略",
            process=proc_name,
            strategy=strategy,
        )
        failed_at_ms = _now_ms()
        _append_upgrade_failed_report(cfg, proc_name, target_version, f"unsupported upgrade strategy: {strategy}", failed_at_ms)
        append_upgrade_event(
            event_file,
            {
                "task_id": str(uuid.uuid4()),
                "process": proc_name,
                "requested_target_version": target_version,
                "target_version": target_version,
                "ok": False,
                "error": f"unsupported upgrade strategy: {strategy}",
                "post_action": "none",
                "post_action_result": "failed",
                "finished_at": now_iso(),
            },
        )
        return 1

    lock_file = _upgrade_lock_file(cfg, proc_name)
    try:
        lock_ctx = FileLock(lock_file)
        lock_ctx.__enter__()
    except Exception as exc:
        log(
            "warn",
            "upgrade.runner.locked",
            "同进程升级任务已在执行，本次 upgrade-runner 跳过",
            process=proc_name,
            target_version=target_version,
            lock_file=lock_file,
            error=str(exc),
        )
        return 0

    ok = False
    err = ""
    resolved_target = target_version
    skipped = False
    xagent_cleanup_paths: list[Path] = []
    try:
        try:
            try:
                if strategy == "xagent_package":
                    ok, err, resolved_target, skipped = execute_xagent_package_upgrade(proc, cfg, target_version)
                elif strategy == "code_server_package":
                    from .code_server_upgrade import execute_code_server_package_upgrade

                    ok, err, resolved_target, skipped = execute_code_server_package_upgrade(proc, cfg, target_version)
                else:
                    ok, err, resolved_target, skipped = execute_meta_package_upgrade(proc, cfg, target_version)
            except Exception as exc:
                ok = False
                err = str(exc)

            failed_at_ms = _now_ms()
            if not ok:
                if strategy == "xagent_package":
                    merge_xagent_startup_timing(
                        cfg,
                        {
                            "startupStatus": "FAILED",
                            "xagentFailedAtMs": failed_at_ms,
                        },
                    )
                    write_xagent_status(
                        cfg,
                        XAGENT_STATUS_ABNORMAL,
                        target_version=resolved_target or target_version,
                    )
                if skipped:
                    log(
                        "warn",
                        "upgrade.runner.skip_latest_start_failed",
                        "当前已是目标版本，但启动确认失败，不上报升级失败",
                        process=proc_name,
                        target_version=resolved_target or target_version,
                        error=err,
                    )
                else:
                    log(
                        "error",
                        "upgrade.runner.failed",
                        "升级失败",
                        process=proc_name,
                        target_version=resolved_target or target_version,
                        error=err,
                    )
                    _append_upgrade_failed_report(cfg, proc_name, resolved_target or target_version, err, failed_at_ms)

            append_upgrade_event(
                event_file,
                {
                    "task_id": str(uuid.uuid4()),
                    "process": proc_name,
                    "requested_target_version": target_version,
                    "target_version": resolved_target or target_version,
                    "ok": ok,
                    "error": err,
                    "post_action": "start",
                    "post_action_result": "skipped" if (skipped and ok) else ("success" if ok else "failed"),
                    "skipped": skipped,
                    "finished_at": now_iso(),
                },
            )
        finally:
            if strategy == "xagent_package":
                xagent_cleanup_paths = _xagent_cleanup_paths(
                    proc.get("upgrade", {}) or {},
                    runtime_root=(cfg.get("runtime", {}) or {}).get("root_dir"),
                    process=proc_name,
                )
            lock_ctx.__exit__(None, None, None)

        if ok and strategy == "xagent_package":
            mode = _xagent_installed_mode(proc)
            if mode:
                ready_at_ms = _log_xagent_init_ready_if_possible(
                    cfg,
                    _xagent_selected_proc(proc, mode),
                    upgraded=not skipped,
                    mode=mode,
                    target_version=resolved_target or target_version,
                )
                if ready_at_ms is not None:
                    merge_xagent_startup_timing(
                        cfg,
                        {
                            "startupStatus": "SUCCESS",
                            "xagentReadyAtMs": ready_at_ms,
                        },
                    )
                else:
                    merge_xagent_startup_timing(
                        cfg,
                        {
                            "startupStatus": "FAILED",
                            "xagentFailedAtMs": _now_ms(),
                        },
                    )
        if ok:
            log("info", "upgrade.runner.success", "升级完成", process=proc_name, target_version=resolved_target or target_version, skipped=skipped)
        return 0 if ok else 1
    finally:
        if strategy == "xagent_package":
            _schedule_xagent_cleanup(xagent_cleanup_paths, process=proc_name)
