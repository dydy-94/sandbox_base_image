from __future__ import annotations

"""PM2 进程管理策略实现。"""

import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any

from ..common import log, run_command, shlex_quote
from ..env_store import read_env_cache
from ..reconcile.env import _env_values_in_file, _resolve_desired_value, expanded_path_from_env_file
from ..runtime_profile import is_rootless_profile
from ..types import CommandResult, ProbeResult
from ..xagent_package import detect_installed_xagent_mode
from .base import ProcessManagerStrategy

PM2_PROBE_TIMEOUT_SECONDS = 5.0
PM2_RETRY_DELAY_SECONDS = 0.5
PM2_SKIP_LATEST_START_TIMEOUT_SECONDS = 15.0


class PM2Strategy(ProcessManagerStrategy):
    manager_name = "pm2"

    def _xagent_mode(self, proc: dict[str, Any]) -> str | None:
        up = proc.get("upgrade", {}) or {}
        if str(up.get("strategy", "")).strip() != "xagent_package":
            return None
        return detect_installed_xagent_mode(up)

    def _xagent_pm2_name(self, proc: dict[str, Any], mode: str | None) -> str | None:
        up = proc.get("upgrade", {}) or {}
        if mode == "source":
            return str(up.get("source_pm2_name", "")).strip() or None
        if mode == "binary":
            return str(up.get("binary_pm2_name", "")).strip() or None
        return None

    def _is_xagent_package(self, proc: dict[str, Any]) -> bool:
        return str((proc.get("upgrade", {}) or {}).get("strategy", "")).strip() == "xagent_package"

    def _xagent_all_pm2_names(self, proc: dict[str, Any]) -> list[str]:
        up = proc.get("upgrade", {}) or {}
        names: list[str] = []
        for value in [
            str(up.get("binary_pm2_name", "xagent")).strip(),
            str(up.get("source_pm2_name", "xagent-dev")).strip(),
        ]:
            if value and value not in names:
                names.append(value)
        return names

    def _xagent_start_command(self, proc: dict[str, Any], mode: str | None) -> str | None:
        up = proc.get("upgrade", {}) or {}
        if mode == "source":
            return str(up.get("source_start_command", "")).strip() or None
        if mode == "binary":
            return str(up.get("binary_start_command", "")).strip() or None
        return None

    def _resolved_pm2_name(self, proc: dict[str, Any]) -> str:
        mode = self._xagent_mode(proc)
        return self._xagent_pm2_name(proc, mode) or proc.get("manager_options", {}).get("pm2_name") or proc["name"]

    def _resolved_start_command(self, proc: dict[str, Any]) -> str:
        mode = self._xagent_mode(proc)
        return self._xagent_start_command(proc, mode) or proc["start_command"]

    def _pm2_env(self, cfg: dict[str, Any]) -> dict[str, str]:
        env = dict(os.environ)
        env["PM2_HOME"] = cfg["runtime"]["pm2_home"]
        env_file = str((cfg.get("runtime", {}) or {}).get("env_file", "/home/x/.bashrc"))
        # 先把 env_file 中声明的变量显式注入，避免依赖 shell source 行为。
        file_values = _env_values_in_file(env_file)
        expanded_path = expanded_path_from_env_file(env_file, env)
        # PATH 单独按声明顺序安全展开，不能直接使用静态解析出的字面量。
        file_values.pop("PATH", None)
        env.update(file_values)
        if expanded_path is not None:
            env["PATH"] = expanded_path
        env_cache = read_env_cache(cfg)
        # 再用配置中的显式值覆盖，保证受纳管变量稳定可控。
        for item in ((cfg.get("env", {}) or {}).get("items", []) or []):
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "")).strip()
            if not key:
                continue
            policy = str(item.get("policy", "")).strip()
            if policy == "service_dynamic":
                if key in file_values:
                    continue
                cached = env_cache.get(key)
                if cached:
                    env[key] = cached
                continue
            apply_phase = str(item.get("apply_phase", "both")).strip()
            desired = _resolve_desired_value(item)
            # bootstrap-only 的动态变量，如果 env_file 中已经有值，
            # 说明本次 bootstrap 已成功注入，应优先使用落盘值；
            # 若 env_file 没有，再回退到当前进程环境中的默认值。
            if apply_phase == "bootstrap" and "value_from_env" in item and key in file_values:
                continue
            if desired is not None:
                env[key] = desired
        return env

    def _pm2_home(self, cfg: dict[str, Any]) -> Path:
        return Path(str((cfg.get("runtime", {}) or {}).get("pm2_home", "/home/x/.pm2"))).expanduser()

    def _pm2_daemon_ready(self, cfg: dict[str, Any]) -> bool:
        pm2_home = self._pm2_home(cfg)
        return (pm2_home / "rpc.sock").exists() and (pm2_home / "pub.sock").exists()

    def _extract_jlist_rows(self, stdout: str) -> list[Any] | None:
        text = (stdout or "").strip()
        if not text:
            return []
        try:
            rows = json.loads(text)
            return rows if isinstance(rows, list) else None
        except json.JSONDecodeError:
            for marker in ('[{"', "[{", "[]"):
                start = text.find(marker)
                if start < 0:
                    continue
                end = text.rfind("]")
                if end < start:
                    continue
                try:
                    rows = json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
                return rows if isinstance(rows, list) else None
            return None

    def _jlist_snapshot(self, cfg: dict[str, Any]) -> tuple[list[Any], str]:
        cache = cfg.setdefault("_pm2_probe_cache", {})
        cache_key = str(self._pm2_home(cfg))
        cached = cache.get(cache_key)
        if isinstance(cached, tuple) and len(cached) == 2:
            return cached

        if not self._pm2_daemon_ready(cfg):
            result = ([], "pm2 daemon not ready")
            cache[cache_key] = result
            return result
        try:
            res = subprocess.run(
                ["pm2", "jlist"],
                text=True,
                capture_output=True,
                env=self._pm2_env(cfg),
                timeout=PM2_PROBE_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            result = ([], "pm2 command not found")
            cache[cache_key] = result
            return result
        except subprocess.TimeoutExpired:
            result = ([], "pm2 jlist timeout")
            cache[cache_key] = result
            return result
        if res.returncode != 0:
            result = ([], res.stderr.strip() or res.stdout.strip() or "pm2 jlist failed")
            cache[cache_key] = result
            return result

        rows = self._extract_jlist_rows(res.stdout)
        if rows is None:
            result = ([], "pm2 jlist 解析失败")
            cache[cache_key] = result
            return result
        result = (rows, "")
        cache[cache_key] = result
        return result

    def _looks_like_pm2_cold_start(self, result: CommandResult) -> bool:
        text = f"{result.stdout}\n{result.stderr}".lower()
        markers = [
            "spawning pm2 daemon",
            "pm2 daemon",
            "god daemon",
            "launching in no daemon mode",
        ]
        return any(marker in text for marker in markers)

    def _run_pm2_command(self, cmd: str, cfg: dict[str, Any], timeout_seconds: float = 60) -> CommandResult:
        if is_rootless_profile(cfg) and not self._pm2_daemon_ready(cfg):
            return CommandResult(
                returncode=1,
                stdout="",
                stderr="rootless pm2 runtime not ready; refuse to spawn a background pm2 daemon",
            )
        env = self._pm2_env(cfg)
        try:
            res = run_command(cmd, timeout=int(timeout_seconds), env=env)
        except subprocess.TimeoutExpired:
            return CommandResult(returncode=124, stdout="", stderr=f"pm2 command timeout after {int(timeout_seconds)} seconds")
        if res.returncode == 0:
            return res
        if not self._looks_like_pm2_cold_start(res):
            return res
        time.sleep(PM2_RETRY_DELAY_SECONDS)
        try:
            return run_command(cmd, timeout=int(timeout_seconds), env=env)
        except subprocess.TimeoutExpired:
            return CommandResult(returncode=124, stdout="", stderr=f"pm2 command timeout after {int(timeout_seconds)} seconds")

    def _pm2_delete_not_found(self, result: CommandResult) -> bool:
        text = f"{result.stdout}\n{result.stderr}".lower()
        return any(marker in text for marker in ["not found", "does not exist", "doesn't exist", "process or namespace not found"])

    def _delete_xagent_names(self, proc: dict[str, Any], cfg: dict[str, Any], *, log_event: str) -> CommandResult:
        first_error: CommandResult | None = None
        last_result = CommandResult(returncode=0, stdout="", stderr="")
        for name in self._xagent_all_pm2_names(proc):
            res = self._run_pm2_command(f"pm2 delete {shlex_quote(name)}", cfg, timeout_seconds=30)
            last_result = res
            if res.returncode == 0 or self._pm2_delete_not_found(res):
                continue
            if first_error is None:
                first_error = res
            log(
                "warn",
                log_event,
                "xagent PM2 进程清理失败",
                pm2_name=name,
                returncode=res.returncode,
                stdout=(res.stdout or "")[-500:],
                stderr=(res.stderr or "")[-500:],
            )
        return first_error or last_result

    def start_xagent(self, proc: dict[str, Any], cfg: dict[str, Any], *, timeout_seconds: float = 60, cleanup_before_start: bool = True) -> CommandResult:
        if cleanup_before_start:
            self._delete_xagent_names(proc, cfg, log_event="pm2.xagent.cleanup_before_start_failed")
        return self._run_pm2_command(self._resolved_start_command(proc), cfg, timeout_seconds=timeout_seconds)

    def _probe_names(self, proc: dict[str, Any]) -> list[str]:
        mode = self._xagent_mode(proc)
        if not self._is_xagent_package(proc):
            return [self._resolved_pm2_name(proc)]
        names: list[str] = []
        for candidate in [
            self._xagent_pm2_name(proc, mode),
            str((proc.get("upgrade", {}) or {}).get("source_pm2_name", "")).strip() or None,
            str((proc.get("upgrade", {}) or {}).get("binary_pm2_name", "")).strip() or None,
        ]:
            if candidate and candidate not in names:
                names.append(candidate)
        return names

    def _instance_details(self, row: dict[str, Any]) -> dict[str, Any]:
        pm2_env = row.get("pm2_env") or {}
        monit = row.get("monit") or {}
        pm_uptime = pm2_env.get("pm_uptime")
        runtime_seconds: int | None = None
        try:
            if pm_uptime is not None:
                runtime_seconds = max(0, int(time.time() - (float(pm_uptime) / 1000.0)))
        except (TypeError, ValueError):
            runtime_seconds = None
        restart_time = pm2_env.get("restart_time")
        return {
            "pm_id": row.get("pm_id"),
            "name": str(row.get("name", "")),
            "pid": row.get("pid"),
            "status": str(pm2_env.get("status") or "").strip() or "UNKNOWN",
            "restart_time": restart_time,
            "pm_uptime": pm_uptime,
            "pm2_status": str(pm2_env.get("status") or "").strip() or "UNKNOWN",
            "pm2_restart_time": restart_time,
            "pm2_uptime": pm_uptime,
            "runtime_seconds": runtime_seconds,
            "memory": monit.get("memory"),
            "cpu": monit.get("cpu"),
        }

    def probe(self, proc: dict[str, Any], cfg: dict[str, Any]) -> ProbeResult:
        rows, error = self._jlist_snapshot(cfg)
        if error:
            return ProbeResult(False, False, False, "ERROR", error)

        names = self._probe_names(proc)
        matches = [
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("name", "")) in names
        ]
        if len(matches) > 1:
            instances = [self._instance_details(row) for row in matches]
            return ProbeResult(
                True,
                False,
                False,
                "DUPLICATE",
                f"multiple pm2 instances found: {len(instances)}",
                {"instances": instances},
            )
        if matches:
            details = self._instance_details(matches[0])
            status = str(details.get("pm2_status") or "UNKNOWN").strip() or "UNKNOWN"
            if status == "online":
                return ProbeResult(True, True, False, status, "ok", details)
            if status in {"launching", "stopping", "waiting restart"}:
                return ProbeResult(True, False, True, status, "transitional", details)
            return ProbeResult(True, False, False, status, "not running", details)
        return ProbeResult(False, False, False, "NOT_FOUND", "process not found")

    def delete_instances(self, proc: dict[str, Any], cfg: dict[str, Any], pm_ids: list[Any]) -> CommandResult:
        normalized: list[str] = []
        for value in pm_ids:
            text = str(value).strip() if value is not None else ""
            if text and text not in normalized:
                normalized.append(text)
        if not normalized or len(normalized) != len(pm_ids):
            return CommandResult(returncode=2, stdout="", stderr="duplicate pm2 instances contain missing or invalid pm_id")
        targets = " ".join(shlex_quote(pm_id) for pm_id in normalized)
        return self._run_pm2_command(f"pm2 delete {targets}", cfg, timeout_seconds=30)

    def start(self, proc: dict[str, Any], cfg: dict[str, Any]) -> CommandResult:
        return self._run_pm2_command(self._resolved_start_command(proc), cfg)

    def start_with_timeout(self, proc: dict[str, Any], cfg: dict[str, Any], timeout_seconds: float) -> CommandResult:
        return self._run_pm2_command(self._resolved_start_command(proc), cfg, timeout_seconds=timeout_seconds)

    def restart(self, proc: dict[str, Any], cfg: dict[str, Any]) -> CommandResult:
        cmd = proc.get("restart_command")
        if cmd:
            return self._run_pm2_command(str(cmd), cfg)
        name = self._resolved_pm2_name(proc)
        return self._run_pm2_command(f"pm2 restart {name} --update-env", cfg)

    def stop(self, proc: dict[str, Any], cfg: dict[str, Any]) -> CommandResult:
        # 删除 PM2 托管项但保留安装文件，确保条件重新匹配后 ensure_exists 能重新启动。
        return self.delete(proc, cfg)

    def delete(self, proc: dict[str, Any], cfg: dict[str, Any]) -> CommandResult:
        if self._is_xagent_package(proc):
            return self._delete_xagent_names(proc, cfg, log_event="pm2.xagent.delete_failed")
        name = self._resolved_pm2_name(proc)
        return self._run_pm2_command(f"pm2 delete {name}", cfg)


def cleanup_rootless_pm2_anchor(cfg: dict[str, Any]) -> bool:
    """删除 pm2-runtime 初始化占位项；socket 未就绪时绝不触发 PM2 daemonize。"""
    if not is_rootless_profile(cfg):
        return True
    strategy = PM2Strategy()
    if not strategy._pm2_daemon_ready(cfg):
        log("warn", "pm2.runtime.not_ready", "rootless PM2 runtime 尚未就绪，暂不清理 anchor")
        return False
    supervisor = ((cfg.get("runtime", {}) or {}).get("supervisor", {}) or {})
    anchor = str(supervisor.get("pm2_anchor_process") or "sandbox-pm2-anchor")
    result = strategy._run_pm2_command(f"pm2 delete {shlex_quote(anchor)}", cfg, timeout_seconds=30)
    if result.returncode == 0 or strategy._pm2_delete_not_found(result):
        log("info", "pm2.runtime.anchor_cleaned", "rootless PM2 runtime anchor 已清理", process=anchor)
        return True
    log(
        "warn",
        "pm2.runtime.anchor_cleanup_failed",
        "rootless PM2 runtime anchor 清理失败",
        process=anchor,
        error=(result.stderr.strip() or result.stdout.strip())[:500],
    )
    return False
