from __future__ import annotations

"""Guard 运行 profile 判定。"""

from typing import Any


LEGACY_PROFILE = "legacy"
ROOTLESS_PROFILE = "rootless"
SUPPORTED_RUNTIME_PROFILES = {LEGACY_PROFILE, ROOTLESS_PROFILE}


def runtime_profile(cfg: dict[str, Any] | None) -> str:
    runtime = ((cfg or {}).get("runtime", {}) or {}) if isinstance(cfg, dict) else {}
    return str(runtime.get("profile") or LEGACY_PROFILE).strip().lower()


def is_rootless_profile(cfg: dict[str, Any] | None) -> bool:
    return runtime_profile(cfg) == ROOTLESS_PROFILE
