from __future__ import annotations

"""状态对象构造与判定。"""

import uuid
from copy import deepcopy
from typing import Any

from .common import now_iso
from .constants import APP_VERSION

PERSISTENT_TOP_LEVEL_KEYS = (
    "report",
    "guard_report",
    "skills_sync",
    "workdir_cleanup",
    "xagent_activity_notify",
    "xagent_heartbeat_control",
)


def _persistent_top_level_state(prev: dict[str, Any]) -> dict[str, Any]:
    preserved: dict[str, Any] = {}
    for key in PERSISTENT_TOP_LEVEL_KEYS:
        value = prev.get(key)
        if isinstance(value, dict):
            preserved[key] = deepcopy(value)
    return preserved


def build_base_state(cfg: dict[str, Any], phase: str, prev: dict[str, Any] | None = None) -> dict[str, Any]:
    """创建每轮基础状态对象。"""
    prev = prev or {}
    state = {
        "daemon_version": APP_VERSION,
        "phase": phase,
        "bootstrap_epoch": prev.get("bootstrap_epoch"),
        "overall_status": "ok",
        "sandbox_status": "starting" if phase == "bootstrapping" else "ready",
        "last_cycle_id": str(uuid.uuid4()),
        "last_cycle_time": now_iso(),
        "summary": {
            "process_total": 0,
            "process_online": 0,
            "process_degraded": 0,
            "process_upgrading": 0,
            "process_failed": 0,
        },
        "env": {"enabled": bool(cfg.get("env", {}).get("enabled", False)), "status": "ok"},
        "processes": {},
        "resources": dict(prev.get("resources", {}) or {}),
        "errors": [],
    }
    state.update(_persistent_top_level_state(prev))
    return state


def finalize_overall_status(state: dict[str, Any]) -> None:
    """根据 summary 汇总 overall_status。"""
    if state.get("phase") == "bootstrapping":
        state["overall_status"] = "degraded" if state.get("errors") else "ok"
        state["sandbox_status"] = "starting"
        return

    failed = int(state["summary"].get("process_failed", 0))
    degraded = int(state["summary"].get("process_degraded", 0))
    upgrading = int(state["summary"].get("process_upgrading", 0))

    if state.get("errors"):
        if failed <= 0 and upgrading <= 0:
            degraded = max(1, degraded)
        else:
            failed = max(1, failed)

    if failed > 0:
        state["overall_status"] = "failed"
        state["sandbox_status"] = "failed"
    elif degraded > 0 or upgrading > 0:
        state["overall_status"] = "degraded"
        state["sandbox_status"] = "upgrading" if upgrading > 0 else "degraded"
    else:
        state["overall_status"] = "ok"
        state["sandbox_status"] = "ready"
