from __future__ import annotations

"""Process applicability selectors based on sandbox identity."""

from dataclasses import dataclass
import os
from typing import Any

from .env_store import read_env_cache

ENABLED_WHEN_SANDBOX_TYPES = "sandbox_types"
ENABLED_WHEN_SANDBOX_PLATFORMS = "sandbox_platforms"
ENABLED_WHEN_ENV = "env"
SUPPORTED_ENABLED_WHEN_KEYS = {
    ENABLED_WHEN_SANDBOX_TYPES,
    ENABLED_WHEN_SANDBOX_PLATFORMS,
    ENABLED_WHEN_ENV,
}


@dataclass(frozen=True)
class ProcessApplicability:
    applicable: bool
    sandbox_type: str
    sandbox_platform: str
    reason: str = ""


def _sandbox_value(cfg: dict[str, Any], key: str, cache: dict[str, str]) -> str:
    value = os.environ.get(key, "").strip()
    if value:
        return value.upper()
    return str(cache.get(key, "")).strip().upper()


def _selector_values(enabled_when: dict[str, Any], key: str) -> list[str] | None:
    if key not in enabled_when:
        return None
    raw = enabled_when.get(key)
    if not isinstance(raw, list):
        return []
    return [str(value or "").strip().upper() for value in raw if str(value or "").strip()]


def _dynamic_env_value(key: str, cache: dict[str, str]) -> str:
    """Read service-dynamic cache first so a running daemon sees updates."""
    if key in cache:
        return str(cache.get(key, "")).strip().upper()
    return str(os.environ.get(key, "")).strip().upper()


def process_applicability(proc: dict[str, Any], cfg: dict[str, Any]) -> ProcessApplicability:
    """Match OR within each selector dimension and AND across dimensions."""
    cache = read_env_cache(cfg)
    sandbox_type = _sandbox_value(cfg, "X_SANDBOX_TYPE", cache)
    sandbox_platform = _sandbox_value(cfg, "X_SANDBOX_PLATFORM", cache)
    enabled_when = proc.get("enabled_when")
    if enabled_when is None:
        return ProcessApplicability(True, sandbox_type, sandbox_platform)
    if not isinstance(enabled_when, dict):
        return ProcessApplicability(False, sandbox_type, sandbox_platform, "enabled_when is invalid")

    type_values = _selector_values(enabled_when, ENABLED_WHEN_SANDBOX_TYPES)
    platform_values = _selector_values(enabled_when, ENABLED_WHEN_SANDBOX_PLATFORMS)
    if type_values is not None and sandbox_type not in type_values:
        return ProcessApplicability(False, sandbox_type, sandbox_platform, "sandbox_type not matched")
    if platform_values is not None and sandbox_platform not in platform_values:
        return ProcessApplicability(False, sandbox_type, sandbox_platform, "sandbox_platform not matched")
    env_selectors = enabled_when.get(ENABLED_WHEN_ENV)
    if env_selectors is not None:
        if not isinstance(env_selectors, dict):
            return ProcessApplicability(False, sandbox_type, sandbox_platform, "enabled_when.env is invalid")
        for key, raw_values in env_selectors.items():
            env_key = str(key or "").strip()
            if not env_key or not isinstance(raw_values, list):
                return ProcessApplicability(False, sandbox_type, sandbox_platform, "enabled_when.env is invalid")
            expected = [
                str(value or "").strip().upper()
                for value in raw_values
                if str(value or "").strip()
            ]
            if _dynamic_env_value(env_key, cache) not in expected:
                return ProcessApplicability(
                    False,
                    sandbox_type,
                    sandbox_platform,
                    f"environment selector not matched: {env_key}",
                )
    return ProcessApplicability(True, sandbox_type, sandbox_platform)


def process_is_applicable(proc: dict[str, Any], cfg: dict[str, Any]) -> bool:
    return process_applicability(proc, cfg).applicable


def inapplicable_process_state(proc: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    result = process_applicability(proc, cfg)
    name = str(proc.get("name", "")).strip()
    manager = str(proc.get("manager", "pm2")).strip() or "pm2"
    return {
        "name": name,
        "manager": manager,
        "manager_type": manager,
        "manager_capability": "disabled",
        "required": False,
        "applicable": False,
        "disabled_reason": "sandbox_selector",
        "sandbox_type": result.sandbox_type,
        "sandbox_platform": result.sandbox_platform,
        "status": "disabled",
        "upgrade_state": "stable",
        "pending_target_version": None,
        "last_action": "none",
        "last_action_result": "skipped",
        "message": result.reason or "process disabled by sandbox selector",
    }
