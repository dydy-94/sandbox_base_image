from __future__ import annotations

"""Built-in skill synchronization support."""

from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
import json
import os
from pathlib import Path
import random
import subprocess
import sys
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
import uuid

from .common import ensure_parent, log, now_iso, prepare_async_child_env, run_command, shlex_quote, write_json_atomic
from .env_store import read_env_cache
from .http_client import http_get_json, http_post_json
from .paths import root_path

LEGACY_BATCH_UPDATE_SCRIPT = "/home/x/skill/batchUpdate.sh"
DEFAULT_SKILL_ENABLED_SANDBOX_TYPES = ["USER"]


@dataclass
class SkillRequest:
    request_id: str
    reason: str
    skills: list[str]
    requested_at: str


def _skills_enabled(cfg: dict[str, Any]) -> bool:
    return bool((cfg.get("skills", {}) or {}).get("enabled", False))


def sandbox_type_for_skills(cfg: dict[str, Any]) -> str:
    text = os.environ.get("X_SANDBOX_TYPE", "").strip()
    if text:
        return text.upper()
    return str(read_env_cache(cfg).get("X_SANDBOX_TYPE", "")).strip().upper()


def enabled_sandbox_types_for_skills(cfg: dict[str, Any]) -> list[str]:
    raw = (cfg.get("skills", {}) or {}).get("enabled_sandbox_types", DEFAULT_SKILL_ENABLED_SANDBOX_TYPES)
    if not isinstance(raw, list):
        return []
    return _unique_skills([str(value or "").strip().upper() for value in raw])


def skill_sync_applicable(cfg: dict[str, Any]) -> bool:
    return _skills_enabled(cfg) and sandbox_type_for_skills(cfg) in enabled_sandbox_types_for_skills(cfg)


def _unique_skills(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        name = str(value or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def append_skill_request(path: str, skills: list[str] | None = None, reason: str = "manual") -> str:
    ensure_parent(path)
    request_id = str(uuid.uuid4())
    payload = {
        "type": "skill_sync",
        "request_id": request_id,
        "reason": str(reason or "manual"),
        "skills": _unique_skills(skills or []),
        "created_at": now_iso(),
    }
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return request_id


def load_skill_requests(path: str) -> list[SkillRequest]:
    p = Path(path)
    if not p.exists():
        return []
    reqs: list[SkillRequest] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            skills_raw = raw.get("skills", [])
            skills = _unique_skills([str(x) for x in skills_raw]) if isinstance(skills_raw, list) else []
            reqs.append(
                SkillRequest(
                    request_id=str(raw.get("request_id") or uuid.uuid4()),
                    reason=str(raw.get("reason") or "manual"),
                    skills=skills,
                    requested_at=str(raw.get("created_at") or ""),
                )
            )
    except Exception:
        return []
    return reqs


def clear_skill_requests(path: str) -> None:
    ensure_parent(path)
    Path(path).write_text("", encoding="utf-8")


def append_skill_done_event(path: str, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def load_skill_done_events(path: str) -> list[dict[str, Any]]:
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
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict) and raw.get("type") == "skill.sync.done":
                events.append(raw)
        p.write_text("", encoding="utf-8")
    except Exception:
        return []
    return events


def read_skill_version(versions_dir: str, name: str) -> str:
    path = Path(versions_dir) / f"{name}.version"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _read_json_object(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def bootstrap_sync_marker_file(cfg: dict[str, Any]) -> str:
    bootstrap_sync = ((cfg.get("skills", {}) or {}).get("bootstrap_sync", {}) or {})
    return str(bootstrap_sync.get("marker_file") or root_path(cfg, "skill_bootstrap_sync.json"))


def _write_bootstrap_marker(cfg: dict[str, Any], marker: dict[str, Any]) -> None:
    payload = {
        key: str(marker.get(key, "")).strip()
        for key in ["first_skipped_at", "last_requested_at"]
        if str(marker.get(key, "")).strip()
    }
    write_json_atomic(bootstrap_sync_marker_file(cfg), payload)


def _bootstrap_marker_baseline(marker: dict[str, Any]) -> datetime | None:
    return _parse_iso(str(marker.get("last_requested_at") or marker.get("first_skipped_at") or ""))


def append_bootstrap_skill_request(cfg: dict[str, Any]) -> bool:
    skills_cfg = cfg.get("skills", {}) or {}
    if not bool(skills_cfg.get("enabled", False)):
        return False
    sandbox_type = sandbox_type_for_skills(cfg)
    enabled_types = enabled_sandbox_types_for_skills(cfg)
    if sandbox_type not in enabled_types:
        return False
    bootstrap_sync = skills_cfg.get("bootstrap_sync", {}) or {}
    if not bool(bootstrap_sync.get("enabled", True)):
        return False

    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    cooldown = max(0, int(bootstrap_sync.get("cooldown_seconds", 600)))
    marker = _read_json_object(bootstrap_sync_marker_file(cfg))
    if not str(marker.get("first_skipped_at", "")).strip():
        _write_bootstrap_marker(cfg, {"first_skipped_at": now_text})
        return False

    baseline = _bootstrap_marker_baseline(marker)
    if baseline is not None and cooldown > 0 and (now - baseline).total_seconds() < cooldown:
        return False

    append_skill_request(str((cfg.get("runtime", {}) or {}).get("skill_request_file", "")), [], reason="bootstrap")
    marker["last_requested_at"] = now_text
    _write_bootstrap_marker(cfg, marker)
    return True


def _parse_skill_manifest(raw: Any) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        raise ValueError("skill manifest response must be object")
    if raw.get("success") is False:
        raise ValueError(f"skill manifest response failed: {raw.get('code') or raw.get('message') or 'unknown'}")
    data = raw.get("data")
    if not isinstance(data, dict):
        raise ValueError("skill manifest response data must be object")
    manifest: dict[str, dict[str, str]] = {}
    for name, item in data.items():
        skill_name = str(name or "").strip()
        if not skill_name:
            raise ValueError("skill manifest contains empty skill name")
        if not isinstance(item, dict):
            raise ValueError(f"skill manifest item must be object: {skill_name}")
        version = str(item.get("version") or "").strip()
        url = str(item.get("downloadUrl") or "").strip()
        if not version or not url:
            raise ValueError(f"skill manifest item missing version/downloadUrl: {skill_name}")
        manifest[skill_name] = {"version": version, "url": url}
    return manifest


def _url_with_query(url: str, key: str, value: str) -> str:
    parts = urlsplit(str(url or "").strip())
    addition = urlencode({key: value})
    query = f"{parts.query}&{addition}" if parts.query else addition
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def skill_sap_id() -> str:
    return os.environ.get("X_SANDBOX_USER_ID", "").strip()


def load_skill_manifest(cfg: dict[str, Any]) -> dict[str, dict[str, str]]:
    skills_cfg = cfg.get("skills", {}) or {}
    manifest_cfg = skills_cfg.get("manifest", {}) or {}
    if str(manifest_cfg.get("type", "http_json")).strip() != "http_json":
        raise ValueError("skills.manifest.type only supports http_json")

    timeout = float(manifest_cfg.get("timeout_seconds", 5))
    fallback_url = str(manifest_cfg.get("url") or "").strip()
    sap_url = str(manifest_cfg.get("sap_url") or "").strip()
    sap_id = skill_sap_id()
    if sap_id and sap_url:
        try:
            raw = http_get_json(_url_with_query(sap_url, "sapId", sap_id), timeout)
            manifest = _parse_skill_manifest(raw)
            if manifest:
                return manifest
        except Exception as exc:
            log(
                "warn",
                "skill.manifest.sap_failed",
                "按 sapId 获取 skill 清单失败，回退公共清单",
                sap_id=sap_id,
                error=str(exc),
            )
    return _parse_skill_manifest(http_get_json(fallback_url, timeout))


def callback_successful_skills(cfg: dict[str, Any], sap_id: str, payload: list[dict[str, Any]]) -> None:
    if not sap_id or not payload:
        return
    callback_cfg = ((cfg.get("skills", {}) or {}).get("callback", {}) or {})
    base_url = str(callback_cfg.get("url") or "").strip().rstrip("/")
    if not base_url:
        return
    timeout = float(callback_cfg.get("timeout_seconds", 5))
    target = f"{base_url}/{quote(sap_id, safe='')}"
    try:
        response = http_post_json(target, payload, timeout)
        if response.get("success") is False:
            raise ValueError(str(response.get("message") or response.get("code") or "callback rejected"))
    except Exception as exc:
        log(
            "warn",
            "skill.callback.failed",
            "skill 安装结果回调失败",
            sap_id=sap_id,
            skills=[item["packageId"] for item in payload],
            error=str(exc),
        )


def resolve_batch_update_script(cfg: dict[str, Any]) -> str:
    configured = str((cfg.get("skills", {}) or {}).get("batch_update_script", "")).strip()
    candidates: list[Path] = []
    if configured:
        p = Path(configured).expanduser()
        candidates.append(p if p.is_absolute() else Path(str((cfg.get("runtime", {}) or {}).get("root_dir", ""))) / p)
    candidates.append(Path(LEGACY_BATCH_UPDATE_SCRIPT))
    for path in candidates:
        if path.exists():
            return str(path)
    return str(candidates[0]) if candidates else LEGACY_BATCH_UPDATE_SCRIPT


def _parse_time_hhmm(value: str, default: datetime_time) -> datetime_time:
    text = str(value or "").strip()
    try:
        hour_s, minute_s = text.split(":", 1)
        hour = int(hour_s)
        minute = int(minute_s)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return datetime_time(hour=hour, minute=minute)
    except Exception:
        pass
    return default


def _normalize_now(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now


def _next_skill_auto_sync_interval_at(now: datetime, auto: dict[str, Any]) -> str:
    interval = max(1, int(auto.get("interval_seconds", 600)))
    jitter = max(0, int(auto.get("jitter_seconds", 0)))
    offset = random.randint(0, jitter) if jitter else 0
    return (now + timedelta(seconds=interval + offset)).astimezone(timezone.utc).isoformat()


def _next_skill_auto_sync_window_at(now: datetime, auto: dict[str, Any]) -> str:
    # Legacy idle-window strategy. Keep this isolated so interval scheduling can evolve independently.
    start_t = _parse_time_hhmm(str(auto.get("window_start", "02:00")), datetime_time(2, 0))
    end_t = _parse_time_hhmm(str(auto.get("window_end", "03:00")), datetime_time(3, 0))
    min_delay = max(0, int(auto.get("min_delay_seconds", 1800)))

    local_now = now.astimezone()
    target_date = local_now.date()
    start_dt = datetime.combine(target_date, start_t, tzinfo=local_now.tzinfo)
    end_dt = datetime.combine(target_date, end_t, tzinfo=local_now.tzinfo)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    if local_now >= start_dt or (start_dt - local_now).total_seconds() < min_delay:
        start_dt += timedelta(days=1)
        end_dt += timedelta(days=1)
    span_seconds = max(0, int((end_dt - start_dt).total_seconds()))
    offset = random.randint(0, span_seconds) if span_seconds else 0
    return (start_dt + timedelta(seconds=offset)).astimezone(timezone.utc).isoformat()


def next_skill_auto_sync_at(now: datetime | None, cfg: dict[str, Any]) -> str:
    now = _normalize_now(now)
    auto = ((cfg.get("skills", {}) or {}).get("auto_sync", {}) or {})
    mode = str(auto.get("mode", "window")).strip().lower() or "window"
    if mode == "interval":
        return _next_skill_auto_sync_interval_at(now, auto)
    return _next_skill_auto_sync_window_at(now, auto)


def skill_auto_schedule_signature(cfg: dict[str, Any]) -> dict[str, Any]:
    auto = ((cfg.get("skills", {}) or {}).get("auto_sync", {}) or {})
    mode = str(auto.get("mode", "window")).strip().lower() or "window"
    if mode == "interval":
        return {
            "mode": "interval",
            "interval_seconds": max(1, int(auto.get("interval_seconds", 600))),
            "jitter_seconds": max(0, int(auto.get("jitter_seconds", 0))),
        }
    return {
        "mode": "window",
        "window_start": str(auto.get("window_start", "02:00")),
        "window_end": str(auto.get("window_end", "03:00")),
        "min_delay_seconds": max(0, int(auto.get("min_delay_seconds", 1800))),
    }


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _merge_requests(reqs: list[SkillRequest]) -> tuple[list[str], str, str]:
    if not reqs:
        return [], "", ""
    request_id = reqs[-1].request_id
    reason = reqs[-1].reason
    if any(not r.skills for r in reqs):
        return [], request_id, reason
    merged: list[str] = []
    for req in reqs:
        merged.extend(req.skills)
    return _unique_skills(merged), request_id, reason


def schedule_skill_sync(cfg_path: str, request_id: str, reason: str, skills: list[str]) -> bool:
    script = Path(__file__).resolve().parent.parent.parent / "sandbox_guard.py"
    skill_args = " ".join(f"--skill {shlex_quote(skill)}" for skill in skills)
    cmd = (
        f"{shlex_quote(sys.executable)} {shlex_quote(str(script))} "
        f"skill-sync-runner --config {shlex_quote(cfg_path)} "
        f"--request-id {shlex_quote(request_id)} --reason {shlex_quote(reason)} {skill_args}"
    ).strip()
    try:
        subprocess.Popen(cmd, shell=True, env=prepare_async_child_env())
        return True
    except Exception as exc:
        log("error", "skill.runner.schedule_failed", "skill 同步 runner 调度失败", request_id=request_id, error=str(exc))
        return False


def reconcile_skill_sync(cfg: dict[str, Any], cfg_path: str, state: dict[str, Any], prev: dict[str, Any]) -> None:
    if not _skills_enabled(cfg):
        return
    sandbox_type = sandbox_type_for_skills(cfg)
    enabled_types = enabled_sandbox_types_for_skills(cfg)
    if sandbox_type not in enabled_types:
        state["skills_sync"] = {
            "enabled": False,
            "disabled_reason": "sandbox_type",
            "sandbox_type": sandbox_type or "",
            "enabled_sandbox_types": enabled_types,
        }
        return
    runtime = cfg.get("runtime", {}) or {}
    skills_cfg = cfg.get("skills", {}) or {}
    skill_state = dict(prev.get("skills_sync", {}) or {})

    done_events = load_skill_done_events(str(runtime.get("skill_event_file", "")))
    if done_events:
        latest = done_events[-1]
        skill_state["running"] = False
        skill_state["last_result"] = {
            "request_id": latest.get("request_id", ""),
            "status": latest.get("status", ""),
            "finished_at": latest.get("finished_at", ""),
            "summary": latest.get("summary", {}),
        }

    running = bool(skill_state.get("running", False))
    started = _parse_iso(str(skill_state.get("started_at", "")))
    timeout = int(((skills_cfg.get("update", {}) or {}).get("running_timeout_seconds", 1800)))
    if running and started is not None and (datetime.now(timezone.utc) - started).total_seconds() > timeout:
        log("warn", "skill.runner.stale", "skill 同步 runner 超时，清理运行态", request_id=skill_state.get("request_id", ""), timeout_seconds=timeout)
        skill_state["running"] = False
        skill_state["last_result"] = {
            "request_id": skill_state.get("request_id", ""),
            "status": "stale",
            "finished_at": now_iso(),
            "summary": {},
        }
        running = False

    auto = skills_cfg.get("auto_sync", {}) or {}
    if bool(auto.get("enabled", True)):
        next_at = _parse_iso(str(skill_state.get("next_auto_sync_at", "")))
        now = datetime.now(timezone.utc)
        schedule_signature = skill_auto_schedule_signature(cfg)
        previous_signature = skill_state.get("auto_schedule")
        if next_at is None:
            skill_state["next_auto_sync_at"] = next_skill_auto_sync_at(now, cfg)
            skill_state["auto_schedule"] = schedule_signature
        elif previous_signature != schedule_signature:
            skill_state["next_auto_sync_at"] = next_skill_auto_sync_at(now, cfg)
            skill_state["auto_schedule"] = schedule_signature
        elif now >= next_at:
            append_skill_request(str(runtime.get("skill_request_file", "")), [], reason="auto")
            skill_state["next_auto_sync_at"] = next_skill_auto_sync_at(now, cfg)
            skill_state["auto_schedule"] = schedule_signature

    if bool(skill_state.get("running", False)):
        state["skills_sync"] = skill_state
        return

    reqs = load_skill_requests(str(runtime.get("skill_request_file", "")))
    if not reqs:
        state["skills_sync"] = skill_state
        return
    skills, request_id, reason = _merge_requests(reqs)
    if schedule_skill_sync(cfg_path, request_id, reason, skills):
        skill_state.update(
            {
                "running": True,
                "request_id": request_id,
                "reason": reason,
                "skills": skills,
                "all": not skills,
                "started_at": now_iso(),
            }
        )
        clear_skill_requests(str(runtime.get("skill_request_file", "")))
    state["skills_sync"] = skill_state


def _script_json(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("batchUpdate output json missing")


def _write_done(cfg: dict[str, Any], request_id: str, status: str, started_at: str, summary: dict[str, Any]) -> None:
    append_skill_done_event(
        str((cfg.get("runtime", {}) or {}).get("skill_event_file", "")),
        {
            "type": "skill.sync.done",
            "request_id": request_id,
            "status": status,
            "started_at": started_at,
            "finished_at": now_iso(),
            "summary": summary,
        },
    )
    log("info" if status in {"success", "skipped"} else "warn", "skill.sync.done", "skill 同步结束", request_id=request_id, status=status, summary=summary)


def run_skill_sync_runner(cfg: dict[str, Any], requested_skills: list[str], request_id: str, reason: str) -> int:
    if not skill_sync_applicable(cfg):
        log("info", "skill.sync.skip_sandbox_type", "当前沙箱类型不启用 skill 同步", request_id=request_id, sandbox_type=sandbox_type_for_skills(cfg) or "-", enabled_sandbox_types=enabled_sandbox_types_for_skills(cfg))
        return 2
    started_at = now_iso()
    requested = _unique_skills(requested_skills)
    log("info", "skill.sync.start", "开始执行 skill 同步", request_id=request_id, reason=reason, skills=requested)
    try:
        manifest = load_skill_manifest(cfg)
        versions_dir = str((cfg.get("skills", {}) or {}).get("versions_dir", "/home/x/plugins-version"))
        selected_names = requested or list(manifest.keys())
        missing = [name for name in selected_names if name not in manifest]
        targets = {name: manifest[name] for name in selected_names if name in manifest}
        current_versions = {name: read_skill_version(versions_dir, name) for name in targets}
        pending = [name for name, item in targets.items() if current_versions.get(name, "") != item["version"]]
        install_status = {
            name: 1 if (Path(versions_dir) / f"{name}.version").exists() else 0
            for name in pending
        }
        install_skills = [name for name in pending if install_status[name] == 0]
        upgrade_skills = [name for name in pending if install_status[name] == 1]
        skipped = len(targets) - len(pending)
        failed: set[str] = set(missing)
        attempts: dict[str, int] = {name: 0 for name in pending}
        success: set[str] = set()
        script = resolve_batch_update_script(cfg)
        update_cfg = (cfg.get("skills", {}) or {}).get("update", {}) or {}
        max_attempts = max(1, int(update_cfg.get("max_attempts", 3)))
        timeout = int(update_cfg.get("timeout_seconds", 900))

        log(
            "info",
            "skill.sync.plan",
            "skill 同步变更计划",
            request_id=request_id,
            install_skills=install_skills,
            upgrade_skills=upgrade_skills,
        )

        remaining = list(pending)
        while remaining:
            for name in remaining:
                attempts[name] = attempts.get(name, 0) + 1
            args: list[str] = []
            for name in remaining:
                item = targets[name]
                args.extend([item["url"], name, item["version"]])
            cmd = "bash " + shlex_quote(script) + " " + " ".join(shlex_quote(arg) for arg in args)
            result = run_command(cmd, timeout=timeout)
            failed_this_round: set[str] = set()
            if result.returncode != 0:
                failed_this_round.update(remaining)
            else:
                try:
                    parsed = _script_json(result.stdout)
                    if str(parsed.get("status", "")).strip() != "success":
                        package_ids = parsed.get("packageIds", [])
                        if isinstance(package_ids, list):
                            failed_this_round.update(str(x) for x in package_ids)
                        else:
                            failed_this_round.update(remaining)
                except Exception:
                    failed_this_round.update(remaining)
            for name in remaining:
                target_version = targets[name]["version"]
                if read_skill_version(versions_dir, name) == target_version and name not in failed_this_round:
                    success.add(name)
                else:
                    failed_this_round.add(name)
            retry = [name for name in remaining if name in failed_this_round and attempts.get(name, 0) < max_attempts]
            failed.update(name for name in remaining if name in failed_this_round and attempts.get(name, 0) >= max_attempts)
            remaining = retry
            if remaining:
                log(
                    "warn",
                    "skill.batch.failed",
                    "部分 skill 安装失败，准备重试",
                    request_id=request_id,
                    skills=remaining,
                    attempts={name: attempts[name] for name in remaining},
                    returncode=result.returncode,
                )

        callback_payload = [
            {
                "packageId": name,
                "versionNum": targets[name]["version"],
                "installStatus": install_status[name],
            }
            for name in pending
            if name in success
        ]
        callback_successful_skills(cfg, skill_sap_id(), callback_payload)

        total = len(selected_names)
        failed_skills = sorted(failed)
        if total == 0 or (skipped == total and not failed_skills):
            status = "skipped"
        elif failed_skills and success:
            status = "partial_failed"
        elif failed_skills:
            status = "failed"
        else:
            status = "success"
        summary = {
            "total": total,
            "success_count": len(success),
            "skipped_count": skipped,
            "failed_count": len(failed_skills),
            "failed_skills": failed_skills,
            "installed_skills": sorted(name for name in success if install_status[name] == 0),
            "upgraded_skills": sorted(name for name in success if install_status[name] == 1),
        }
        _write_done(cfg, request_id, status, started_at, summary)
        return 0 if status in {"success", "skipped"} else 1
    except Exception as exc:
        summary = {"total": 0, "success_count": 0, "failed_count": 0, "failed_skills": [], "error": str(exc)}
        _write_done(cfg, request_id, "failed", started_at, summary)
        return 1
