from __future__ import annotations

"""配置加载与校验模块。"""

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .paths import DEFAULT_ENV_FILE, DEFAULT_PM2_HOME, DEFAULT_ROOT_DIR, root_path, runtime_tmp_dir, tmp_path
from .process_applicability import SUPPORTED_ENABLED_WHEN_KEYS
from .runtime_profile import LEGACY_PROFILE, ROOTLESS_PROFILE, SUPPORTED_RUNTIME_PROFILES

DEFAULT_SKILL_BATCH_UPDATE_SCRIPT = "scripts/skill/batchUpdate.sh"


def _base_url_from_endpoint(endpoint: Any) -> str:
    text = str(endpoint or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except Exception:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _ensure_dict(data: Any, path: str) -> dict[str, Any]:
    """确保配置顶层为对象。"""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"配置顶层必须为对象: {path}")
    return data


def _read_json(path: str) -> dict[str, Any]:
    """读取 JSON 配置。"""
    p = Path(path)
    return _ensure_dict(json.loads(p.read_text(encoding="utf-8")), path)


def read_config(path: str) -> dict[str, Any]:
    """读取配置文件并返回对象。

    约定：
    - 仅支持 JSON（.json）
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"配置文件不存在: {path}")
    if p.suffix.lower() != ".json":
        raise ValueError(f"仅支持 JSON 配置文件（.json）: {path}")
    return _read_json(path)


def apply_defaults(config: dict[str, Any], app_version: str) -> dict[str, Any]:
    """补全配置默认值，减少上层判空逻辑。"""
    cfg = dict(config)
    had_activity_notify_config = "xagent_activity_notify" in cfg
    had_heartbeat_control_config = "xagent_heartbeat_control" in cfg
    cfg.setdefault("bootstrap", {})
    cfg.setdefault("daemon", {})
    cfg.setdefault("runtime", {})
    cfg.setdefault("env", {"enabled": False, "items": []})
    cfg.setdefault("self_update", {})
    cfg.setdefault("registry", {})
    cfg.setdefault("redis", {})
    cfg.setdefault("report", {})
    cfg.setdefault("skills", {})
    cfg.setdefault("xagent_activity_notify", {})
    cfg.setdefault("xagent_heartbeat_control", {})
    cfg.setdefault("processes", [])
    cfg["bootstrap"].setdefault("scripts", [])

    cfg["daemon"].setdefault("interval_seconds", 5)
    cfg["daemon"].setdefault("upgrade_event_missing_timeout_seconds", 90)
    if not isinstance(cfg["daemon"].get("workdir_cleanup", {}), dict):
        cfg["daemon"]["workdir_cleanup"] = {"_invalid_type": True}
    else:
        cfg["daemon"].setdefault("workdir_cleanup", {})
        cfg["daemon"]["workdir_cleanup"].setdefault("enabled", True)
        cfg["daemon"]["workdir_cleanup"].setdefault("interval_seconds", 3600)
        cfg["daemon"]["workdir_cleanup"].setdefault("min_age_seconds", 3600)
    if not isinstance(cfg["daemon"].get("pm2_log_cleanup", {}), dict):
        cfg["daemon"]["pm2_log_cleanup"] = {"_invalid_type": True}
    else:
        cfg["daemon"].setdefault("pm2_log_cleanup", {})
        cfg["daemon"]["pm2_log_cleanup"].setdefault("enabled", True)
        cfg["daemon"]["pm2_log_cleanup"].setdefault("interval_seconds", 3600)
        cfg["daemon"]["pm2_log_cleanup"].setdefault("max_size_mb", 20)
    cfg["daemon"].setdefault("upgrade_retry", {})
    base_delay = int(cfg["daemon"]["interval_seconds"])
    cfg["daemon"]["upgrade_retry"].setdefault("base_delay_seconds", base_delay)
    cfg["daemon"]["upgrade_retry"].setdefault("max_delay_seconds", base_delay * 5)
    cfg["daemon"]["upgrade_retry"].setdefault("max_retries", 3)

    cfg["runtime"].setdefault("root_dir", DEFAULT_ROOT_DIR)
    cfg["runtime"]["root_dir"] = str(cfg["runtime"]["root_dir"]).rstrip("/")
    cfg["runtime"].setdefault("profile", LEGACY_PROFILE)
    profile = str(cfg["runtime"].get("profile") or LEGACY_PROFILE).strip().lower()
    cfg["runtime"]["profile"] = profile
    cfg["runtime"].setdefault("execution_user", "x" if profile == ROOTLESS_PROFILE else "root")
    cfg["runtime"].setdefault("default_run_as", "x" if profile == ROOTLESS_PROFILE else "root")
    cfg["runtime"].setdefault("tmp_dir", runtime_tmp_dir(cfg))
    cfg["runtime"]["tmp_dir"] = str(cfg["runtime"]["tmp_dir"]).rstrip("/")
    cfg["runtime"].setdefault("pm2_home", DEFAULT_PM2_HOME)
    cfg["runtime"].setdefault("env_file", DEFAULT_ENV_FILE)
    cfg["runtime"].setdefault("state_file", root_path(cfg, "state.json"))
    cfg["runtime"].setdefault("event_file", root_path(cfg, "events", "upgrade_events.jsonl"))
    cfg["runtime"].setdefault("upgrade_request_file", root_path(cfg, "events", "upgrade_requests.jsonl"))
    cfg["runtime"].setdefault("env_cache_file", root_path(cfg, "env.json"))
    cfg["runtime"].setdefault("env_request_file", root_path(cfg, "events", "env_requests.jsonl"))
    cfg["runtime"].setdefault("xagent_status_file", root_path(cfg, "xagent_status.json"))
    cfg["runtime"].setdefault("skill_request_file", root_path(cfg, "events", "skill_requests.jsonl"))
    cfg["runtime"].setdefault("skill_event_file", root_path(cfg, "events", "skill_events.jsonl"))
    cfg["runtime"].setdefault("env_lock_file", root_path(cfg, "locks", "env.lock"))
    cfg["runtime"].setdefault("bootstrap_lock_file", tmp_path(cfg, "sandbox_guard_bootstrap.lock"))
    cfg["daemon"].setdefault("lock_file", root_path(cfg, "locks", "daemon.lock"))
    cfg["runtime"].setdefault("supervisor", {})
    cfg["runtime"]["supervisor"].setdefault("ctl_bin", "supervisorctl")
    cfg["runtime"]["supervisor"].setdefault("ctl_conf", "/opt/gem/supervisord.conf")
    cfg["runtime"]["supervisor"].setdefault("conf_dir", "/opt/gem/supervisord")
    cfg["runtime"]["supervisor"].setdefault("daemon_program", "sandbox-daemon")
    cfg["runtime"]["supervisor"].setdefault("launcher_program", "sandbox-guard-launcher")
    cfg["runtime"]["supervisor"].setdefault("daemon_autostart", False)
    cfg["runtime"]["supervisor"].setdefault("manage_program_conf", profile != ROOTLESS_PROFILE)
    cfg["runtime"]["supervisor"].setdefault("pm2_runtime_program", "sandbox-pm2-runtime")
    cfg["runtime"]["supervisor"].setdefault("pm2_anchor_process", "sandbox-pm2-anchor")

    if isinstance(cfg.get("registry"), dict):
        cfg["registry"].setdefault("oras_bin", "")
        cfg["registry"].setdefault("oras_host", "")
        cfg["registry"].setdefault("oras_user", "")
        cfg["registry"].setdefault("oras_password", "")

    cfg["redis"].setdefault("enabled", False)
    cfg["redis"].setdefault("nodes", ["127.0.0.1:6379"])
    cfg["redis"].setdefault("password", "")
    cfg["redis"].setdefault("username", "")
    cfg["redis"].setdefault("connect_timeout_seconds", 1.0)
    cfg["redis"].setdefault("command_timeout_seconds", 2.0)
    cfg["redis"].setdefault("max_attempts", 3)
    cfg["redis"].setdefault("max_redirects", 5)

    if isinstance(cfg.get("report"), dict):
        legacy_report_endpoint = str(cfg["report"].get("endpoint", "") or "").strip()
        had_report_types = isinstance(cfg["report"].get("types"), dict)
        cfg["report"].setdefault("enabled", False)
        cfg["report"].setdefault("endpoint", "")
        cfg["report"].setdefault("base_url", _base_url_from_endpoint(cfg["report"].get("endpoint", "")))
        cfg["report"].setdefault("timeout_seconds", 3)
        cfg["report"].setdefault("interval_seconds", 60)
        cfg["report"].setdefault("request_file", root_path(cfg, "events", "report_requests.jsonl"))
        cfg["report"].setdefault("lock_file", root_path(cfg, "locks", "report.lock"))
        if not isinstance(cfg["report"].get("types"), dict):
            cfg["report"]["types"] = {}
        cfg["report"]["types"].setdefault("SANDBOX_STATUS", {})
        cfg["report"]["types"]["SANDBOX_STATUS"].setdefault("enabled", True)
        if legacy_report_endpoint and not had_report_types:
            cfg["report"]["types"]["SANDBOX_STATUS"].setdefault("endpoint", legacy_report_endpoint)
        cfg["report"]["types"]["SANDBOX_STATUS"].setdefault("path", "/v1/sandbox/report/status")
        cfg["report"]["types"]["SANDBOX_STATUS"].setdefault("interval_seconds", cfg["report"].get("interval_seconds", 60))
        cfg["report"]["types"].setdefault("UPGRADE_FAILED", {})
        cfg["report"]["types"]["UPGRADE_FAILED"].setdefault("enabled", False)
        cfg["report"]["types"]["UPGRADE_FAILED"].setdefault("path", "/v1/sandbox/report/upgrade-failed")
        cfg["report"]["types"].setdefault("STARTUP_TIMING", {})
        cfg["report"]["types"]["STARTUP_TIMING"].setdefault("enabled", False)
        cfg["report"]["types"]["STARTUP_TIMING"].setdefault("path", "/v1/sandbox/report/startup-timing")

    if isinstance(cfg.get("skills"), dict):
        cfg["skills"].setdefault("enabled", False)
        cfg["skills"].setdefault("enabled_sandbox_types", ["USER"])
        cfg["skills"].setdefault("plugins_dir", "/home/x/plugins")
        cfg["skills"].setdefault("versions_dir", "/home/x/plugins-version")
        cfg["skills"].setdefault("batch_update_script", DEFAULT_SKILL_BATCH_UPDATE_SCRIPT)
        if not isinstance(cfg["skills"].get("manifest"), dict):
            cfg["skills"]["manifest"] = {}
        cfg["skills"].setdefault("manifest", {})
        cfg["skills"]["manifest"].setdefault("type", "http_json")
        cfg["skills"]["manifest"].setdefault("url", "")
        cfg["skills"]["manifest"].setdefault("sap_url", "")
        cfg["skills"]["manifest"].setdefault("timeout_seconds", 5)
        if not isinstance(cfg["skills"].get("callback"), dict):
            cfg["skills"]["callback"] = {}
        cfg["skills"].setdefault("callback", {})
        cfg["skills"]["callback"].setdefault("url", "")
        cfg["skills"]["callback"].setdefault("timeout_seconds", 5)
        if not isinstance(cfg["skills"].get("bootstrap_sync"), dict):
            cfg["skills"]["bootstrap_sync"] = {}
        cfg["skills"].setdefault("bootstrap_sync", {})
        cfg["skills"]["bootstrap_sync"].setdefault("enabled", True)
        cfg["skills"]["bootstrap_sync"].setdefault("cooldown_seconds", 600)
        cfg["skills"]["bootstrap_sync"].setdefault("marker_file", root_path(cfg, "skill_bootstrap_sync.json"))
        if not isinstance(cfg["skills"].get("auto_sync"), dict):
            cfg["skills"]["auto_sync"] = {}
        cfg["skills"].setdefault("auto_sync", {})
        cfg["skills"]["auto_sync"].setdefault("enabled", True)
        cfg["skills"]["auto_sync"].setdefault("mode", "interval")
        cfg["skills"]["auto_sync"].setdefault("window_start", "02:00")
        cfg["skills"]["auto_sync"].setdefault("window_end", "03:00")
        cfg["skills"]["auto_sync"].setdefault("min_delay_seconds", 1800)
        cfg["skills"]["auto_sync"].setdefault("interval_seconds", 600)
        cfg["skills"]["auto_sync"].setdefault("jitter_seconds", 60)
        if not isinstance(cfg["skills"].get("update"), dict):
            cfg["skills"]["update"] = {}
        cfg["skills"].setdefault("update", {})
        cfg["skills"]["update"].setdefault("timeout_seconds", 900)
        cfg["skills"]["update"].setdefault("max_attempts", 3)
        cfg["skills"]["update"].setdefault("running_timeout_seconds", 1800)

    if isinstance(cfg.get("xagent_activity_notify"), dict):
        # New release configs enable this explicitly. A completely missing block
        # remains disabled for compatibility with older environment configs.
        cfg["xagent_activity_notify"].setdefault("enabled", had_activity_notify_config)
        cfg["xagent_activity_notify"].setdefault("interval_seconds", 3600)
        cfg["xagent_activity_notify"].setdefault("base_url", "")
        cfg["xagent_activity_notify"].setdefault("timeout_seconds", 3)

    if isinstance(cfg.get("xagent_heartbeat_control"), dict):
        # A completely missing block remains disabled for legacy configs.
        cfg["xagent_heartbeat_control"].setdefault("enabled", had_heartbeat_control_config)
        cfg["xagent_heartbeat_control"].setdefault("xagent_process", "xagent")
        cfg["xagent_heartbeat_control"].setdefault("heartbeat_process", "nacos-heartbeat")
        cfg["xagent_heartbeat_control"].setdefault("timeout_seconds", 3)

    registry_cfg = cfg.get("registry", {}) if isinstance(cfg.get("registry"), dict) else {}
    for key in ["oras_bin", "oras_host", "oras_user", "oras_password"]:
        value = registry_cfg.get(key)
        if value is not None and str(value).strip():
            cfg["self_update"].setdefault(key, value)
    cfg["self_update"].setdefault("work_dir", root_path(cfg, "work", "self_update"))
    for proc in cfg.get("processes", []):
        if not isinstance(proc, dict):
            continue
        stability_policy = proc.get("stability_policy")
        if isinstance(stability_policy, dict) and bool(stability_policy.get("enabled", False)):
            stability_policy.setdefault("startup_grace_seconds", 30)
            stability_policy.setdefault("stable_reset_seconds", 300)
            stability_policy.setdefault("backoff_seconds", [0, 30, 120, 600, 1800])
        upgrade = proc.get("upgrade")
        if not isinstance(upgrade, dict):
            continue
        for key in ["oras_bin", "oras_host", "oras_user", "oras_password"]:
            value = registry_cfg.get(key)
            if value is not None and str(value).strip():
                upgrade.setdefault(key, value)
        name = str(proc.get("name") or "process").strip() or "process"
        upgrade.setdefault("work_dir", root_path(cfg, "work", name))
        if str(upgrade.get("strategy", "")).strip() == "xagent_package":
            if not isinstance(upgrade.get("ready_wait", {}), dict):
                upgrade["ready_wait"] = {"_invalid_type": True}
            else:
                upgrade.setdefault("ready_wait", {})
                upgrade["ready_wait"].setdefault("timeout_seconds", 15)
                upgrade["ready_wait"].setdefault("interval_seconds", 0.3)
                upgrade["ready_wait"].setdefault("request_timeout_seconds", 1)
        if str(upgrade.get("strategy", "")).strip() == "code_server_package":
            upgrade.setdefault("outer_root", "code-server-deploy")
            upgrade.setdefault("inner_package_file", "code-server.zip")
            upgrade.setdefault("inner_root", "code-server")
            upgrade.setdefault("deploy_root", "/home/x/.code-server")
            upgrade.setdefault("go_bin_dir", "/home/x/go/bin")
            upgrade.setdefault("dependency_timeout_seconds", 1800)
            upgrade.setdefault("deploy_timeout_seconds", 300)
            upgrade.setdefault("event_missing_timeout_seconds", 2400)
            if not isinstance(upgrade.get("ready_wait", {}), dict):
                upgrade["ready_wait"] = {"_invalid_type": True}
            else:
                upgrade.setdefault("ready_wait", {})
                upgrade["ready_wait"].setdefault("timeout_seconds", 15)
                upgrade["ready_wait"].setdefault("interval_seconds", 0.3)

    return cfg


def validate_config(config: dict[str, Any]) -> list[str]:
    """做最小可运行校验。

    说明：
    - v1 先保证基础字段完整；
    - 复杂语义校验可后续增强。
    """
    errors: list[str] = []
    if not isinstance(config.get("processes"), list):
        errors.append("processes 必须为数组")
    if not isinstance(config.get("daemon"), dict):
        errors.append("daemon 必须为对象")
    if not isinstance(config.get("runtime"), dict):
        errors.append("runtime 必须为对象")
    else:
        runtime_cfg = config.get("runtime", {}) or {}
        if not str(runtime_cfg.get("root_dir", "")).strip():
            errors.append("runtime.root_dir 不能为空")
        if "tmp_dir" in runtime_cfg and not str(runtime_cfg.get("tmp_dir", "")).strip():
            errors.append("runtime.tmp_dir 不能为空")
        profile = str(runtime_cfg.get("profile") or LEGACY_PROFILE).strip().lower()
        if profile not in SUPPORTED_RUNTIME_PROFILES:
            errors.append("runtime.profile 仅支持 legacy / rootless")
        if not str(runtime_cfg.get("execution_user", "")).strip():
            errors.append("runtime.execution_user 不能为空")
        supervisor_cfg = runtime_cfg.get("supervisor", {}) or {}
        if not isinstance(supervisor_cfg, dict):
            errors.append("runtime.supervisor 必须为对象")
        elif profile == ROOTLESS_PROFILE:
            if str(runtime_cfg.get("execution_user", "")).strip() != "x":
                errors.append("runtime.profile=rootless 时 runtime.execution_user 必须为 x")
            if bool(supervisor_cfg.get("manage_program_conf", True)):
                errors.append("runtime.profile=rootless 时 runtime.supervisor.manage_program_conf 必须为 false")
    if not isinstance(config.get("bootstrap"), dict):
        errors.append("bootstrap 必须为对象")
    if not isinstance(config.get("self_update", {}), dict):
        errors.append("self_update 必须为对象")
    if not isinstance(config.get("registry", {}), dict):
        errors.append("registry 必须为对象")
    if not isinstance(config.get("redis", {}), dict):
        errors.append("redis 必须为对象")
    if not isinstance(config.get("report", {}), dict):
        errors.append("report 必须为对象")
    if not isinstance(config.get("skills", {}), dict):
        errors.append("skills 必须为对象")
    if not isinstance(config.get("xagent_activity_notify", {}), dict):
        errors.append("xagent_activity_notify 必须为对象")
    if not isinstance(config.get("xagent_heartbeat_control", {}), dict):
        errors.append("xagent_heartbeat_control 必须为对象")
    if "artifacts" in config:
        errors.append("artifacts 已废弃，本版本不支持")
    if "dependencies" in config:
        errors.append("dependencies 已废弃，本版本不支持")

    daemon_cfg = config.get("daemon", {}) or {}
    if not isinstance(daemon_cfg.get("workdir_cleanup", {}), dict) or bool((daemon_cfg.get("workdir_cleanup", {}) or {}).get("_invalid_type", False)):
        errors.append("daemon.workdir_cleanup 必须为对象")
    pm2_log_cleanup = daemon_cfg.get("pm2_log_cleanup", {})
    if not isinstance(pm2_log_cleanup, dict) or bool((pm2_log_cleanup or {}).get("_invalid_type", False)):
        errors.append("daemon.pm2_log_cleanup 必须为对象")
    else:
        defaults = {"interval_seconds": 3600, "max_size_mb": 20}
        for key, default in defaults.items():
            try:
                if int(pm2_log_cleanup.get(key, default)) <= 0:
                    errors.append(f"daemon.pm2_log_cleanup.{key} 必须为正整数")
            except Exception:
                errors.append(f"daemon.pm2_log_cleanup.{key} 必须为正整数")
    if "start_command" in daemon_cfg:
        errors.append("daemon.start_command 已废弃，请通过 runtime.supervisor.daemon_program 托管 daemon")
    if "start_manager" in daemon_cfg:
        errors.append("daemon.start_manager 已废弃，本版本固定使用 supervisor 托管 daemon")
    if "self_update" in daemon_cfg:
        errors.append("daemon.self_update 已废弃，本版本不支持 daemon 自更新")

    skills_cfg = config.get("skills", {}) or {}
    if isinstance(skills_cfg, dict):
        if bool(skills_cfg.get("enabled", False)):
            manifest = skills_cfg.get("manifest", {}) or {}
            update = skills_cfg.get("update", {}) or {}
            auto_sync = skills_cfg.get("auto_sync", {}) or {}
            bootstrap_sync = skills_cfg.get("bootstrap_sync", {}) or {}
            if not isinstance(manifest, dict):
                errors.append("skills.manifest 必须为对象")
                manifest = {}
            if not isinstance(update, dict):
                errors.append("skills.update 必须为对象")
                update = {}
            if not isinstance(auto_sync, dict):
                errors.append("skills.auto_sync 必须为对象")
                auto_sync = {}
            if not isinstance(bootstrap_sync, dict):
                errors.append("skills.bootstrap_sync 必须为对象")
                bootstrap_sync = {}
            enabled_types = skills_cfg.get("enabled_sandbox_types", ["USER"])
            if not isinstance(enabled_types, list):
                errors.append("skills.enabled_sandbox_types 必须为数组")
            elif not any(str(value or "").strip() for value in enabled_types):
                errors.append("skills.enabled_sandbox_types 不能为空")
            if str(manifest.get("type", "")).strip() != "http_json":
                errors.append("skills.manifest.type 仅支持 http_json")
            if not str(manifest.get("url", "")).strip():
                errors.append("skills.manifest.url 不能为空")
            if not str(skills_cfg.get("batch_update_script", "")).strip():
                errors.append("skills.batch_update_script 不能为空")
            if not str(skills_cfg.get("versions_dir", "")).strip():
                errors.append("skills.versions_dir 不能为空")
            mode = str(auto_sync.get("mode", "window")).strip().lower() or "window"
            if mode not in {"window", "interval"}:
                errors.append("skills.auto_sync.mode 仅支持 window / interval")
            try:
                if int(update.get("max_attempts", 0)) < 1:
                    errors.append("skills.update.max_attempts 必须 >= 1")
            except Exception:
                errors.append("skills.update.max_attempts 必须为整数")
            for key in ["timeout_seconds", "running_timeout_seconds"]:
                try:
                    if int(update.get(key, 0)) <= 0:
                        errors.append(f"skills.update.{key} 必须为正数")
                except Exception:
                    errors.append(f"skills.update.{key} 必须为整数")
            try:
                if int(auto_sync.get("min_delay_seconds", 0)) < 0:
                    errors.append("skills.auto_sync.min_delay_seconds 必须 >= 0")
            except Exception:
                errors.append("skills.auto_sync.min_delay_seconds 必须为整数")
            try:
                if int(auto_sync.get("interval_seconds", 0)) <= 0:
                    errors.append("skills.auto_sync.interval_seconds 必须为正数")
            except Exception:
                errors.append("skills.auto_sync.interval_seconds 必须为整数")
            try:
                if int(auto_sync.get("jitter_seconds", 0)) < 0:
                    errors.append("skills.auto_sync.jitter_seconds 必须 >= 0")
            except Exception:
                errors.append("skills.auto_sync.jitter_seconds 必须为整数")
            try:
                if int(bootstrap_sync.get("cooldown_seconds", 0)) < 0:
                    errors.append("skills.bootstrap_sync.cooldown_seconds 必须 >= 0")
            except Exception:
                errors.append("skills.bootstrap_sync.cooldown_seconds 必须为整数")

    report_cfg = config.get("report", {}) or {}
    if isinstance(report_cfg, dict):
        if bool(report_cfg.get("enabled", False)):
            has_base = bool(str(report_cfg.get("base_url", "")).strip())
            has_endpoint = bool(str(report_cfg.get("endpoint", "")).strip())
            if not has_base and not has_endpoint:
                errors.append("report.base_url 不能为空")
        if not isinstance(report_cfg.get("types", {}), dict):
            errors.append("report.types 必须为对象")
        try:
            if float(report_cfg.get("timeout_seconds", 0)) <= 0:
                errors.append("report.timeout_seconds 必须为正数")
        except Exception:
            errors.append("report.timeout_seconds 必须为数字")
        try:
            if int(report_cfg.get("interval_seconds", 0)) < -1:
                errors.append("report.interval_seconds 必须 >= -1")
        except Exception:
            errors.append("report.interval_seconds 必须为整数")
        types = report_cfg.get("types", {}) if isinstance(report_cfg.get("types", {}), dict) else {}
        status_type = types.get("SANDBOX_STATUS", {}) if isinstance(types, dict) else {}
        if isinstance(status_type, dict):
            try:
                if int(status_type.get("interval_seconds", report_cfg.get("interval_seconds", 60))) < -1:
                    errors.append("report.types.SANDBOX_STATUS.interval_seconds 必须 >= -1")
            except Exception:
                errors.append("report.types.SANDBOX_STATUS.interval_seconds 必须为整数")

    activity_cfg = config.get("xagent_activity_notify", {}) or {}
    if isinstance(activity_cfg, dict):
        if bool(activity_cfg.get("enabled", False)):
            base_url = str(activity_cfg.get("base_url", "")).strip()
            if not base_url:
                errors.append("xagent_activity_notify.base_url 不能为空")
            else:
                parts = urlsplit(base_url)
                if parts.scheme not in {"http", "https"} or not parts.netloc:
                    errors.append("xagent_activity_notify.base_url 必须为有效的 HTTP(S) 地址")
            xagent_gate_url = ""
            for process in config.get("processes", []):
                if not isinstance(process, dict) or str(process.get("name", "")).strip() != "xagent":
                    continue
                upgrade = process.get("upgrade", {}) or {}
                gate = upgrade.get("idle_gate", {}) if isinstance(upgrade, dict) else {}
                if isinstance(gate, dict):
                    xagent_gate_url = str(gate.get("url", "")).strip()
                break
            if not xagent_gate_url:
                errors.append("xagent_activity_notify 启用时必须配置 processes[xagent].upgrade.idle_gate.url")
        try:
            if int(activity_cfg.get("interval_seconds", 0)) <= 0:
                errors.append("xagent_activity_notify.interval_seconds 必须为正整数")
        except Exception:
            errors.append("xagent_activity_notify.interval_seconds 必须为整数")
        try:
            timeout_seconds = float(activity_cfg.get("timeout_seconds", 0))
            if timeout_seconds <= 0 or timeout_seconds > 3:
                errors.append("xagent_activity_notify.timeout_seconds 必须大于 0 且不超过 3")
        except Exception:
            errors.append("xagent_activity_notify.timeout_seconds 必须为数字")

    heartbeat_control = config.get("xagent_heartbeat_control", {}) or {}
    if isinstance(heartbeat_control, dict):
        try:
            timeout_seconds = float(heartbeat_control.get("timeout_seconds", 0))
            if timeout_seconds <= 0 or timeout_seconds > 3:
                errors.append("xagent_heartbeat_control.timeout_seconds 必须大于 0 且不超过 3")
        except Exception:
            errors.append("xagent_heartbeat_control.timeout_seconds 必须为数字")
        if bool(heartbeat_control.get("enabled", False)):
            xagent_name = str(heartbeat_control.get("xagent_process", "")).strip()
            heartbeat_name = str(heartbeat_control.get("heartbeat_process", "")).strip()
            if not xagent_name:
                errors.append("xagent_heartbeat_control.xagent_process 不能为空")
            if not heartbeat_name:
                errors.append("xagent_heartbeat_control.heartbeat_process 不能为空")
            process_map = {
                str(process.get("name", "")).strip(): process
                for process in config.get("processes", [])
                if isinstance(process, dict) and str(process.get("name", "")).strip()
            }
            if xagent_name and xagent_name not in process_map:
                errors.append(f"xagent_heartbeat_control 未找到 xagent 进程: {xagent_name}")
            heartbeat_process = process_map.get(heartbeat_name)
            if heartbeat_name and heartbeat_process is None:
                errors.append(f"xagent_heartbeat_control 未找到心跳进程: {heartbeat_name}")
            elif isinstance(heartbeat_process, dict):
                health = heartbeat_process.get("health_check", {})
                health_url = str(health.get("url", "")).strip() if isinstance(health, dict) else ""
                if not health_url:
                    errors.append(
                        f"xagent_heartbeat_control 启用时必须配置 processes[{heartbeat_name}].health_check.url"
                    )
                else:
                    parts = urlsplit(health_url)
                    if parts.scheme not in {"http", "https"} or not parts.netloc:
                        errors.append(
                            f"processes[{heartbeat_name}].health_check.url 必须为有效的 HTTP(S) 地址"
                        )

    scripts = (config.get("bootstrap", {}) or {}).get("scripts", [])
    if not isinstance(scripts, list):
        errors.append("bootstrap.scripts 必须为数组")
    else:
        for idx, s in enumerate(scripts):
            if not isinstance(s, dict):
                errors.append(f"bootstrap.scripts[{idx}] 必须为对象")
                continue
            if not s.get("command") and not s.get("path"):
                errors.append(f"bootstrap.scripts[{idx}] 缺少 command/path")

    for idx, p in enumerate(config.get("processes", [])):
        if not isinstance(p, dict):
            errors.append(f"processes[{idx}] 必须为对象")
            continue
        if "name" not in p:
            errors.append(f"processes[{idx}] 缺少 name")
        if "start_command" not in p:
            errors.append(f"process[{p.get('name', idx)}] 缺少 start_command")
        p.setdefault("manager", "pm2")
        manager = str(p.get("manager", "")).strip()
        if manager not in {"pm2", "supervisor", "direct"}:
            errors.append(f"process[{p.get('name', idx)}].manager 仅支持 pm2 / supervisor / direct")
        p.setdefault("required", True)
        p.setdefault("preferred_run_as", config["runtime"].get("default_run_as", "root"))
        p.setdefault("allow_fallback", False)
        p.setdefault("recover_cooldown_seconds", 15)
        p.setdefault("manager_options", {})
        if not isinstance(p.get("manager_options"), dict):
            errors.append(f"process[{p.get('name', idx)}].manager_options 必须为对象")
            p["manager_options"] = {}
        if manager == "direct":
            direct_options = p["manager_options"]
            try:
                if float(direct_options.get("stop_timeout_seconds", 5)) <= 0:
                    errors.append(
                        f"process[{p.get('name', idx)}].manager_options.stop_timeout_seconds 必须大于 0"
                    )
            except Exception:
                errors.append(
                    f"process[{p.get('name', idx)}].manager_options.stop_timeout_seconds 必须为数字"
                )
            for key in ["pid_file", "working_dir", "stdout_file", "stderr_file", "run_as"]:
                if key in direct_options and not str(direct_options.get(key, "")).strip():
                    errors.append(f"process[{p.get('name', idx)}].manager_options.{key} 不能为空")
        p.setdefault("upgrade", {"enabled": False, "strategy": "meta_package"})
        enabled_when = p.get("enabled_when")
        if enabled_when is not None:
            process_label = p.get("name", idx)
            if not isinstance(enabled_when, dict):
                errors.append(f"process[{process_label}].enabled_when 必须为对象")
            else:
                unknown_keys = sorted(set(enabled_when) - SUPPORTED_ENABLED_WHEN_KEYS)
                if unknown_keys:
                    errors.append(
                        f"process[{process_label}].enabled_when 包含未知字段: {', '.join(unknown_keys)}"
                    )
                for key in sorted(SUPPORTED_ENABLED_WHEN_KEYS):
                    if key not in enabled_when:
                        continue
                    if key == "env":
                        env_selectors = enabled_when.get(key)
                        if not isinstance(env_selectors, dict):
                            errors.append(f"process[{process_label}].enabled_when.env 必须为对象")
                            continue
                        if not env_selectors:
                            errors.append(f"process[{process_label}].enabled_when.env 不能为空")
                            continue
                        env_cfg = config.get("env", {})
                        env_items = env_cfg.get("items", []) if isinstance(env_cfg, dict) else []
                        dynamic_keys = {
                            str(item.get("key", "")).strip()
                            for item in (env_items or [])
                            if isinstance(item, dict)
                            and str(item.get("policy", "")).strip() == "service_dynamic"
                        }
                        for env_key, values in env_selectors.items():
                            selector_key = str(env_key or "").strip()
                            if not selector_key:
                                errors.append(
                                    f"process[{process_label}].enabled_when.env 包含空变量名"
                                )
                            elif selector_key not in dynamic_keys:
                                errors.append(
                                    f"process[{process_label}].enabled_when.env.{selector_key} "
                                    "必须在 env.items 中声明为 service_dynamic"
                                )
                            if not isinstance(values, list) or not values or any(
                                not isinstance(value, str) or not value.strip() for value in values
                            ):
                                errors.append(
                                    f"process[{process_label}].enabled_when.env.{selector_key or '<empty>'} "
                                    "必须为非空字符串数组"
                                )
                        continue
                    values = enabled_when.get(key)
                    if not isinstance(values, list):
                        errors.append(f"process[{process_label}].enabled_when.{key} 必须为数组")
                    elif not values or any(
                        not isinstance(value, str) or not value.strip() for value in values
                    ):
                        errors.append(f"process[{process_label}].enabled_when.{key} 必须为非空字符串数组")
        stability_policy = p.get("stability_policy")
        if stability_policy is not None:
            if not isinstance(stability_policy, dict):
                errors.append(f"process[{p.get('name', idx)}].stability_policy 必须为对象")
            else:
                for key in ["startup_grace_seconds", "stable_reset_seconds"]:
                    if key in stability_policy:
                        try:
                            if int(stability_policy.get(key)) < 0:
                                errors.append(f"process[{p.get('name', idx)}].stability_policy.{key} 必须 >= 0")
                        except Exception:
                            errors.append(f"process[{p.get('name', idx)}].stability_policy.{key} 必须为整数")
                if "backoff_seconds" in stability_policy:
                    raw_backoff = stability_policy.get("backoff_seconds")
                    if not isinstance(raw_backoff, list) or not raw_backoff:
                        errors.append(f"process[{p.get('name', idx)}].stability_policy.backoff_seconds 必须为非空数组")
                    else:
                        for value in raw_backoff:
                            try:
                                if int(value) < 0:
                                    errors.append(f"process[{p.get('name', idx)}].stability_policy.backoff_seconds 必须全部 >= 0")
                                    break
                            except Exception:
                                errors.append(f"process[{p.get('name', idx)}].stability_policy.backoff_seconds 必须全部为整数")
                                break
        if not isinstance(p.get("upgrade"), dict):
            errors.append(f"process[{p.get('name', idx)}].upgrade 必须为对象")
            continue
        p["upgrade"].setdefault("enabled", False)
        p["upgrade"].setdefault("strategy", "meta_package")
        strategy = str(p["upgrade"].get("strategy", "")).strip()
        if "min_free_disk_mb" in p["upgrade"]:
            try:
                if int(p["upgrade"].get("min_free_disk_mb")) < 0:
                    errors.append(f"process[{p.get('name', idx)}].upgrade.min_free_disk_mb 必须 >= 0")
            except Exception:
                errors.append(f"process[{p.get('name', idx)}].upgrade.min_free_disk_mb 必须为整数")
        if strategy == "xagent_package":
            ready_wait = p["upgrade"].get("ready_wait")
            if ready_wait is not None and (
                not isinstance(ready_wait, dict) or bool((ready_wait or {}).get("_invalid_type", False))
            ):
                errors.append(f"process[{p.get('name', idx)}].upgrade.ready_wait 必须为对象")
            elif isinstance(ready_wait, dict):
                for key in ["timeout_seconds", "interval_seconds", "request_timeout_seconds"]:
                    try:
                        if float(ready_wait.get(key, 0)) <= 0:
                            errors.append(f"process[{p.get('name', idx)}].upgrade.ready_wait.{key} 必须大于 0")
                    except Exception:
                        errors.append(f"process[{p.get('name', idx)}].upgrade.ready_wait.{key} 必须为数字")
        if strategy == "code_server_package":
            if manager != "pm2":
                errors.append(f"process[{p.get('name', idx)}].upgrade.strategy=code_server_package 仅支持 pm2 manager")
            for key in [
                "meta_ref",
                "outer_root",
                "inner_package_file",
                "inner_root",
                "deploy_root",
                "go_bin_dir",
                "current_version_file",
            ]:
                if not str(p["upgrade"].get(key, "")).strip():
                    errors.append(f"process[{p.get('name', idx)}].upgrade.{key} 不能为空")
            ready_wait = p["upgrade"].get("ready_wait")
            if ready_wait is not None and (
                not isinstance(ready_wait, dict) or bool((ready_wait or {}).get("_invalid_type", False))
            ):
                errors.append(f"process[{p.get('name', idx)}].upgrade.ready_wait 必须为对象")
            elif isinstance(ready_wait, dict):
                for key in ["timeout_seconds", "interval_seconds"]:
                    try:
                        if float(ready_wait.get(key, 0)) <= 0:
                            errors.append(f"process[{p.get('name', idx)}].upgrade.ready_wait.{key} 必须大于 0")
                    except Exception:
                        errors.append(f"process[{p.get('name', idx)}].upgrade.ready_wait.{key} 必须为数字")
            for key in ["dependency_timeout_seconds", "deploy_timeout_seconds", "event_missing_timeout_seconds"]:
                try:
                    if int(p["upgrade"].get(key, 0)) <= 0:
                        errors.append(f"process[{p.get('name', idx)}].upgrade.{key} 必须大于 0")
                except Exception:
                    errors.append(f"process[{p.get('name', idx)}].upgrade.{key} 必须为整数")
        if p["upgrade"].get("enabled", False):
            if strategy not in {"meta_package", "xagent_package", "code_server_package"}:
                errors.append(
                    f"process[{p.get('name', idx)}].upgrade.strategy "
                    "仅支持 meta_package / xagent_package / code_server_package"
                )
            if p["upgrade"].get("mode") or p["upgrade"].get("command") or p["upgrade"].get("steps"):
                errors.append(f"process[{p.get('name', idx)}] 检测到旧升级字段(mode/command/steps)，本版本已废弃")

    # daemon 启动建议由 supervisor 单独托管，不应放入 processes 列表。
    daemon_program = (
        (
            (config.get("runtime", {}) or {})
            .get("supervisor", {})
            .get("daemon_program", "sandbox-daemon")
        )
        or "sandbox-daemon"
    )
    for idx, p in enumerate(config.get("processes", [])):
        if isinstance(p, dict) and str(p.get("name", "")).strip() == str(daemon_program):
            errors.append(
                f"processes[{idx}] name={daemon_program} 与 daemon_program 冲突；"
                "daemon 不应加入 processes，需由 supervisor 单独托管"
            )

    return errors
