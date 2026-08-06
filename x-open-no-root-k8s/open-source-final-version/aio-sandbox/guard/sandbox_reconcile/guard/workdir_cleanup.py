from __future__ import annotations

"""Upgrade workdir cleanup helpers."""

import os
import shutil
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import log, now_iso
from .process_applicability import process_is_applicable


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_safe_stage_dir(base_dir: Path, candidate: Path) -> bool:
    if not candidate.name.startswith("stage_"):
        return False
    try:
        mode = candidate.lstat().st_mode
    except FileNotFoundError:
        return False
    except Exception:
        return False
    if not stat.S_ISDIR(mode):
        return False
    try:
        base_resolved = base_dir.resolve()
        candidate_resolved = candidate.resolve()
    except Exception:
        return False
    base_text = str(base_resolved)
    candidate_text = str(candidate_resolved)
    return candidate_text == base_text or candidate_text.startswith(base_text + os.sep)


def cleanup_stage_dirs(
    work_dir: Path | str,
    *,
    min_age_seconds: int,
    max_delete: int | None = None,
    process: str = "",
    reason: str = "scheduled",
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Delete stale stage_* directories directly under one upgrade workdir."""
    base_dir = Path(work_dir).expanduser()
    result: dict[str, Any] = {
        "work_dir": str(base_dir),
        "deleted": 0,
        "failed": 0,
        "skipped": 0,
        "deleted_paths": [],
        "failed_paths": [],
    }
    if not base_dir.exists() or not base_dir.is_dir():
        return result

    threshold_ts = (time.time() if now_ts is None else now_ts) - max(0, int(min_age_seconds))
    deleted_paths: list[str] = []
    failed_paths: list[str] = []
    deleted = 0
    failed = 0
    skipped = 0

    try:
        candidates = sorted(base_dir.iterdir(), key=lambda item: item.name)
    except Exception as exc:
        log("warn", "workdir.cleanup.scan_failed", "扫描升级工作目录失败", process=process, work_dir=str(base_dir), error=str(exc))
        result["failed"] = 1
        result["failed_paths"] = [str(base_dir)]
        return result

    for candidate in candidates:
        if max_delete is not None and deleted >= max_delete:
            break
        if not _is_safe_stage_dir(base_dir, candidate):
            continue
        try:
            mtime = candidate.stat().st_mtime
        except Exception:
            skipped += 1
            continue
        if mtime > threshold_ts:
            skipped += 1
            continue
        try:
            shutil.rmtree(candidate)
            deleted += 1
            deleted_paths.append(str(candidate))
        except Exception as exc:
            failed += 1
            failed_paths.append(str(candidate))
            log(
                "warn",
                "workdir.cleanup.delete_failed",
                "清理升级 stage 目录失败",
                process=process,
                reason=reason,
                path=str(candidate),
                error=str(exc),
            )

    if deleted:
        log(
            "info",
            "workdir.cleanup.stage_deleted",
            "已清理升级 stage 历史目录",
            process=process,
            reason=reason,
            work_dir=str(base_dir),
            deleted=deleted,
            paths=deleted_paths[:5],
        )
    result.update(
        {
            "deleted": deleted,
            "failed": failed,
            "skipped": skipped,
            "deleted_paths": deleted_paths,
            "failed_paths": failed_paths,
        }
    )
    return result


def _empty_result(work_dir: Path | str) -> dict[str, Any]:
    return {
        "work_dir": str(Path(work_dir).expanduser()),
        "deleted": 0,
        "failed": 0,
        "skipped": 0,
        "deleted_paths": [],
        "failed_paths": [],
    }


def _delete_path(path: Path, *, process: str, reason: str) -> bool:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except FileNotFoundError:
        return True
    except Exception as exc:
        log(
            "warn",
            "workdir.cleanup.delete_failed",
            "清理升级工作目录内容失败",
            process=process,
            reason=reason,
            path=str(path),
            error=str(exc),
        )
        return False


def cleanup_pkg_contents(
    work_dir: Path | str,
    *,
    min_age_seconds: int,
    process: str = "",
    reason: str = "scheduled",
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Delete stale contents under one upgrade workdir/pkg directory."""
    base_dir = Path(work_dir).expanduser()
    pkg_dir = base_dir / "pkg"
    result = _empty_result(pkg_dir)
    if not pkg_dir.exists() or not pkg_dir.is_dir() or pkg_dir.is_symlink():
        return result

    threshold_ts = (time.time() if now_ts is None else now_ts) - max(0, int(min_age_seconds))
    deleted_paths: list[str] = []
    failed_paths: list[str] = []
    deleted = 0
    failed = 0
    skipped = 0

    try:
        candidates = sorted(pkg_dir.iterdir(), key=lambda item: item.name)
    except Exception as exc:
        log("warn", "workdir.cleanup.scan_failed", "扫描升级 pkg 目录失败", process=process, work_dir=str(pkg_dir), error=str(exc))
        result["failed"] = 1
        result["failed_paths"] = [str(pkg_dir)]
        return result

    try:
        base_resolved = pkg_dir.resolve()
    except Exception:
        result["failed"] = 1
        result["failed_paths"] = [str(pkg_dir)]
        return result

    for candidate in candidates:
        try:
            candidate_resolved = candidate.resolve()
        except Exception:
            skipped += 1
            continue
        candidate_text = str(candidate_resolved)
        base_text = str(base_resolved)
        if candidate_text != base_text and not candidate_text.startswith(base_text + os.sep):
            skipped += 1
            continue
        try:
            mtime = candidate.stat().st_mtime
        except Exception:
            skipped += 1
            continue
        if mtime > threshold_ts:
            skipped += 1
            continue
        if _delete_path(candidate, process=process, reason=reason):
            deleted += 1
            deleted_paths.append(str(candidate))
        else:
            failed += 1
            failed_paths.append(str(candidate))

    if deleted:
        log(
            "info",
            "workdir.cleanup.pkg_deleted",
            "已清理升级 pkg 历史内容",
            process=process,
            reason=reason,
            work_dir=str(pkg_dir),
            deleted=deleted,
            paths=deleted_paths[:5],
        )
    result.update(
        {
            "deleted": deleted,
            "failed": failed,
            "skipped": skipped,
            "deleted_paths": deleted_paths,
            "failed_paths": failed_paths,
        }
    )
    return result


def cleanup_upgrade_workdir(
    work_dir: Path | str,
    *,
    min_age_seconds: int,
    process: str = "",
    reason: str = "scheduled",
) -> dict[str, Any]:
    """Delete stale stage_* dirs and pkg contents under one upgrade workdir."""
    stage_result = cleanup_stage_dirs(work_dir, min_age_seconds=min_age_seconds, process=process, reason=reason)
    pkg_result = cleanup_pkg_contents(work_dir, min_age_seconds=min_age_seconds, process=process, reason=reason)
    return {
        "work_dir": str(Path(work_dir).expanduser()),
        "deleted": int(stage_result.get("deleted", 0)) + int(pkg_result.get("deleted", 0)),
        "failed": int(stage_result.get("failed", 0)) + int(pkg_result.get("failed", 0)),
        "skipped": int(stage_result.get("skipped", 0)) + int(pkg_result.get("skipped", 0)),
        "deleted_paths": list(stage_result.get("deleted_paths", [])) + list(pkg_result.get("deleted_paths", [])),
        "failed_paths": list(stage_result.get("failed_paths", [])) + list(pkg_result.get("failed_paths", [])),
    }


def cleanup_current_upgrade_workdirs(stage_dir: Path, pkg_dir: Path, *, process: str = "") -> None:
    """Clean files created by one upgrade attempt."""
    if stage_dir.exists() and not _delete_path(stage_dir, process=process, reason="upgrade_cleanup"):
        return
    if pkg_dir.exists():
        _delete_path(pkg_dir, process=process, reason="upgrade_cleanup")
    try:
        pkg_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log("warn", "workdir.cleanup.pkg_recreate_failed", "重建升级 pkg 目录失败", process=process, path=str(pkg_dir), error=str(exc))


def reconcile_workdir_cleanup(cfg: dict[str, Any], state: dict[str, Any], prev_state: dict[str, Any]) -> None:
    cleanup_cfg = (cfg.get("daemon", {}) or {}).get("workdir_cleanup", {}) or {}
    enabled = bool(cleanup_cfg.get("enabled", True))
    payload: dict[str, Any] = {
        "enabled": enabled,
        "status": "skipped",
        "last_run_at": (prev_state.get("workdir_cleanup", {}) or {}).get("last_run_at"),
        "deleted": 0,
        "failed": 0,
        "message": "",
    }
    state["workdir_cleanup"] = payload
    if not enabled:
        payload["message"] = "disabled"
        return

    interval_seconds = max(1, int(cleanup_cfg.get("interval_seconds", 3600)))
    min_age_seconds = max(1, int(cleanup_cfg.get("min_age_seconds", 3600)))
    last_run_at = _parse_iso(str(payload.get("last_run_at") or ""))
    now = datetime.now(timezone.utc)
    if last_run_at is None:
        payload["last_run_at"] = now_iso()
        payload["message"] = "initialized"
        return
    if last_run_at is not None and (now - last_run_at).total_seconds() < interval_seconds:
        payload["message"] = "interval not due"
        return

    deleted_total = 0
    failed_total = 0
    details: list[dict[str, Any]] = []
    for proc in cfg.get("processes", []) or []:
        if not isinstance(proc, dict):
            continue
        if not process_is_applicable(proc, cfg):
            continue
        upgrade = proc.get("upgrade", {}) or {}
        work_dir = str(upgrade.get("work_dir", "")).strip()
        if not work_dir:
            continue
        proc_state = (state.get("processes", {}) or {}).get(str(proc.get("name", "")), {}) or {}
        if str(proc_state.get("upgrade_state", "")).strip() in {"pending", "upgrading"}:
            continue
        result = cleanup_upgrade_workdir(
            work_dir,
            min_age_seconds=min_age_seconds,
            process=str(proc.get("name", "")),
            reason="daemon_scheduled",
        )
        deleted_total += int(result.get("deleted", 0))
        failed_total += int(result.get("failed", 0))
        if result.get("deleted") or result.get("failed"):
            details.append(
                {
                    "process": str(proc.get("name", "")),
                    "work_dir": work_dir,
                    "deleted": result.get("deleted", 0),
                    "failed": result.get("failed", 0),
                }
            )

    payload["status"] = "ok" if failed_total == 0 else "degraded"
    payload["last_run_at"] = now_iso()
    payload["deleted"] = deleted_total
    payload["failed"] = failed_total
    payload["details"] = details
    payload["message"] = f"deleted={deleted_total}, failed={failed_total}"
