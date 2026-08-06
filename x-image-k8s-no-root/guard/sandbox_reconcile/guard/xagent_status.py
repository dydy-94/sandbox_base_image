from __future__ import annotations

"""Lightweight xagent startup status helpers."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

from .common import ensure_parent
from .readiness import check_process_readiness

XAGENT_STATUS_UNREADY = "UNREADY"
XAGENT_STATUS_CHECKING_UPDATE = "CHECKING_UPDATE"
XAGENT_STATUS_UPGRADING = "UPGRADING"
XAGENT_STATUS_STARTING = "STARTING"
XAGENT_STATUS_READY = "READY"
XAGENT_STATUS_ABNORMAL = "ABNORMAL"

VALID_XAGENT_STATUSES = {
    XAGENT_STATUS_UNREADY,
    XAGENT_STATUS_CHECKING_UPDATE,
    XAGENT_STATUS_UPGRADING,
    XAGENT_STATUS_STARTING,
    XAGENT_STATUS_READY,
    XAGENT_STATUS_ABNORMAL,
}

DEFAULT_XAGENT_STATUS_MAX_AGE_SECONDS = 600


@dataclass
class XAgentStatusResult:
    status: str
    updated_at_ms: int
    target_version: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "updatedAtMs": self.updated_at_ms,
            "targetVersion": self.target_version,
        }


def xagent_status_file(cfg: dict[str, Any]) -> Path:
    runtime = cfg.get("runtime", {}) or {}
    configured = str(runtime.get("xagent_status_file", "")).strip()
    if configured:
        return Path(configured)
    root_dir = Path(str(runtime.get("root_dir") or "/home/x/.daemon"))
    return root_dir / "xagent_status.json"


def write_xagent_status(
    cfg: dict[str, Any],
    status: str,
    *,
    target_version: str = "",
) -> None:
    normalized = str(status or "").strip().upper()
    if normalized not in VALID_XAGENT_STATUSES:
        return
    path = xagent_status_file(cfg)
    now_ms = int(time.time() * 1000)
    payload = {
        "status": normalized,
        "updatedAtMs": now_ms,
        "targetVersion": str(target_version or ""),
    }
    try:
        ensure_parent(str(path))
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        return


def clear_xagent_status(cfg: dict[str, Any]) -> None:
    try:
        xagent_status_file(cfg).unlink(missing_ok=True)
    except Exception:
        return


def _read_status_file(cfg: dict[str, Any]) -> XAgentStatusResult | None:
    path = xagent_status_file(cfg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    status = str(data.get("status") or "").strip().upper()
    if status not in VALID_XAGENT_STATUSES:
        return None
    try:
        updated_at_ms = int(data.get("updatedAtMs") or 0)
    except Exception:
        updated_at_ms = 0
    return XAgentStatusResult(
        status=status,
        updated_at_ms=updated_at_ms,
        target_version=str(data.get("targetVersion") or ""),
    )


def get_xagent_status(cfg: dict[str, Any], *, max_age_seconds: int = DEFAULT_XAGENT_STATUS_MAX_AGE_SECONDS) -> XAgentStatusResult:
    readiness = check_process_readiness(cfg, "xagent")
    if readiness.ready:
        return XAgentStatusResult(
            status=XAGENT_STATUS_READY,
            updated_at_ms=int(time.time() * 1000),
        )

    file_status = _read_status_file(cfg)
    if file_status is None or file_status.status == XAGENT_STATUS_READY:
        return XAgentStatusResult(
            status=XAGENT_STATUS_UNREADY,
            updated_at_ms=0,
        )

    now_ms = int(time.time() * 1000)
    max_age_ms = max(1, int(max_age_seconds)) * 1000
    if file_status.updated_at_ms <= 0 or now_ms - file_status.updated_at_ms > max_age_ms:
        return XAgentStatusResult(
            status=XAGENT_STATUS_UNREADY,
            updated_at_ms=file_status.updated_at_ms,
            target_version=file_status.target_version,
        )
    return file_status
