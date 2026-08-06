from __future__ import annotations

"""Shared xagent running-session probe helpers."""

from typing import Any

from .http_client import HttpJsonError, http_get_json
from .process_applicability import process_is_applicable


def parse_total_field(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("total must be integer")
    total = int(str(value).strip())
    if total < 0:
        raise ValueError("total must be non-negative")
    return total


def probe_xagent_running_sessions(gate: dict[str, Any]) -> tuple[bool, int | None, str]:
    url = str(gate.get("url", "")).strip()
    if not url:
        return False, None, "xagent runningSessions url is empty"
    try:
        timeout_seconds = float(gate.get("timeout_seconds", 2))
    except Exception:
        timeout_seconds = 2.0
    timeout_seconds = max(0.1, timeout_seconds)
    configured_headers = gate.get("headers")
    headers = configured_headers if isinstance(configured_headers, dict) else {}
    try:
        payload = http_get_json(url, timeout_seconds=timeout_seconds, headers=headers)
        if "total" not in payload:
            return False, None, "runningSessions response missing total"
        return True, parse_total_field(payload.get("total")), ""
    except (HttpJsonError, ValueError, TypeError) as exc:
        return False, None, str(exc)


def configured_xagent_session_gate(cfg: dict[str, Any]) -> dict[str, Any] | None:
    for proc in cfg.get("processes", []):
        if not isinstance(proc, dict) or str(proc.get("name", "")).strip() != "xagent":
            continue
        if not process_is_applicable(proc, cfg):
            continue
        upgrade = proc.get("upgrade", {}) or {}
        gate = upgrade.get("idle_gate") if isinstance(upgrade, dict) else None
        if isinstance(gate, dict) and str(gate.get("url", "")).strip():
            return gate
    return None


def probe_configured_xagent_sessions(cfg: dict[str, Any]) -> tuple[bool, int | None, str]:
    gate = configured_xagent_session_gate(cfg)
    if gate is None:
        return False, None, "xagent runningSessions endpoint is not configured"
    return probe_xagent_running_sessions(gate)
