from __future__ import annotations

"""Supervisor 进程管理策略实现。"""

import subprocess
from typing import Any

from ..common import run_command, shlex_quote
from ..types import CommandResult, ProbeResult
from .base import ProcessManagerStrategy


class SupervisorStrategy(ProcessManagerStrategy):
    manager_name = "supervisor"

    def _conf(self, proc: dict[str, Any], cfg: dict[str, Any]) -> str:
        runtime_sup = (cfg.get("runtime", {}) or {}).get("supervisor", {}) or {}
        conf = proc.get("manager_options", {}).get("supervisor_conf") or runtime_sup.get("ctl_conf")
        return str(conf) if conf else ""

    def _base_cmd(self, proc: dict[str, Any], cfg: dict[str, Any]) -> str:
        runtime_sup = (cfg.get("runtime", {}) or {}).get("supervisor", {}) or {}
        ctl_bin = str(runtime_sup["ctl_bin"])
        conf = self._conf(proc, cfg)
        return f"{ctl_bin} -c {shlex_quote(str(conf))}" if conf else ctl_bin

    def _program(self, proc: dict[str, Any]) -> str:
        return proc.get("manager_options", {}).get("supervisor_program") or proc["name"]

    def _parse_status_output(self, text: str) -> dict[str, tuple[str, str]]:
        status_map: dict[str, tuple[str, str]] = {}
        for raw_line in (text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            status_map[parts[0]] = (parts[1], line)
        return status_map

    def _status_map(self, proc: dict[str, Any], cfg: dict[str, Any]) -> tuple[dict[str, tuple[str, str]], str]:
        cache = cfg.setdefault("_supervisor_probe_cache", {})
        conf = self._conf(proc, cfg)
        cache_key = conf or "__default__"
        cached = cache.get(cache_key)
        if isinstance(cached, tuple) and len(cached) == 2:
            return cached

        base = self._base_cmd(proc, cfg)
        try:
            out = run_command(f"{base} status", timeout=15)
        except subprocess.TimeoutExpired:
            result = ({}, "supervisorctl status timeout")
            cache[cache_key] = result
            return result

        text = ((out.stdout or "").strip() + ("\n" + (out.stderr or "").strip() if (out.stderr or "").strip() else "")).strip()
        status_map = self._parse_status_output(text)
        if out.returncode != 0 and not status_map:
            result = ({}, text or "supervisorctl 执行失败")
            cache[cache_key] = result
            return result
        result = (status_map, "")
        cache[cache_key] = result
        return result

    def probe(self, proc: dict[str, Any], cfg: dict[str, Any]) -> ProbeResult:
        prog = self._program(proc)
        status_map, error = self._status_map(proc, cfg)
        if error:
            return ProbeResult(False, False, False, "ERROR", error)
        raw, text = status_map.get(str(prog), ("UNKNOWN", f"{prog} UNKNOWN"))
        if raw == "RUNNING":
            return ProbeResult(True, True, False, raw, text)
        if raw in {"STARTING", "STOPPING"}:
            return ProbeResult(True, False, True, raw, text)
        if raw in {"STOPPED", "EXITED", "BACKOFF", "FATAL", "UNKNOWN"}:
            return ProbeResult(True, False, False, raw, text)
        return ProbeResult(True, False, False, raw, text)

    def start(self, proc: dict[str, Any], cfg: dict[str, Any]) -> CommandResult:
        base = self._base_cmd(proc, cfg)
        prog = self._program(proc)
        return run_command(f"{base} start {shlex_quote(str(prog))}", timeout=60)

    def restart(self, proc: dict[str, Any], cfg: dict[str, Any]) -> CommandResult:
        base = self._base_cmd(proc, cfg)
        prog = self._program(proc)
        return run_command(f"{base} restart {shlex_quote(str(prog))}", timeout=60)

    def stop(self, proc: dict[str, Any], cfg: dict[str, Any]) -> CommandResult:
        base = self._base_cmd(proc, cfg)
        prog = self._program(proc)
        return run_command(f"{base} stop {shlex_quote(str(prog))}", timeout=60)
