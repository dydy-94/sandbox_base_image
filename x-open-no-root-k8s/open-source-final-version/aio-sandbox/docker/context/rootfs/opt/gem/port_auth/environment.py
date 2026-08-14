from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


SERVICE_ENV_PATH = Path("/home/x/.daemon/runtime/env/service_env.json")
BASHRC_PATH = Path("/home/x/.bashrc")
TARGET_KEYS = ("X_SANDBOX_TYPE", "X_SANDBOX_USER_ID")
ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?(X_SANDBOX_TYPE|X_SANDBOX_USER_ID)\s*=\s*(.*?)\s*$"
)
UNSAFE_UNQUOTED_CHARS = frozenset(";$`|&()<> \\t")


@dataclass(frozen=True)
class SandboxContext:
    sandbox_type: str
    user_id: str


def _as_text(value: object) -> str:
    if value is None or isinstance(value, (bool, dict, list)):
        return ""
    return str(value).strip()


def _read_service_env(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("service env must be a JSON object")
    return {key: _as_text(payload.get(key)) for key in TARGET_KEYS}


def _parse_static_shell_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        inner = value[1:-1]
        if value[0] == '"' and any(char in inner for char in ("$", "`", "\\")):
            return ""
        return inner
    if any(char in value for char in UNSAFE_UNQUOTED_CHARS):
        return ""
    return value


def _read_bashrc(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = ASSIGNMENT_RE.match(line)
        if not match:
            continue
        value = _parse_static_shell_value(match.group(2))
        if value:
            values[match.group(1)] = value
    return values


class SandboxContextLoader:
    def __init__(
        self,
        *,
        service_env_path: Path = SERVICE_ENV_PATH,
        bashrc_path: Path = BASHRC_PATH,
        process_env: Mapping[str, str] | None = None,
        retry_seconds: float = 1.0,
    ) -> None:
        self._service_env_path = service_env_path
        self._bashrc_path = bashrc_path
        self._process_env = process_env if process_env is not None else os.environ
        self._retry_seconds = retry_seconds
        self._next_retry_at = 0.0
        self._warning_deadlines: dict[str, float] = {}
        self._cached: SandboxContext | None = None
        self._logger = logging.getLogger("sandbox-port-auth")

    def get(self) -> SandboxContext | None:
        if self._cached is not None:
            return self._cached

        now = time.monotonic()
        if now < self._next_retry_at:
            return None
        self._next_retry_at = now + self._retry_seconds

        values: dict[str, str] = {}
        self._fill_missing(values, self._service_env_path, _read_service_env)
        self._fill_missing(values, self._bashrc_path, _read_bashrc)
        for key in TARGET_KEYS:
            if not values.get(key):
                values[key] = _as_text(self._process_env.get(key))

        sandbox_type = values.get("X_SANDBOX_TYPE", "").strip().upper()
        user_id = values.get("X_SANDBOX_USER_ID", "").strip()
        if not sandbox_type or (sandbox_type == "USER" and not user_id):
            self._warn_limited(
                "incomplete_context",
                "sandbox context unavailable; port authorization is bypassed until retry",
            )
            return None

        self._cached = SandboxContext(sandbox_type=sandbox_type, user_id=user_id)
        self._logger.info("sandbox context cached for port authorization: type=%s", sandbox_type)
        return self._cached

    def _fill_missing(
        self,
        values: dict[str, str],
        path: Path,
        reader: Callable[[Path], dict[str, str]],
    ) -> None:
        if all(values.get(key) for key in TARGET_KEYS):
            return
        try:
            loaded = reader(path)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self._warn_limited(
                f"read:{path}", "cannot read sandbox environment from %s: %s", path, exc
            )
            return
        for key in TARGET_KEYS:
            if not values.get(key) and loaded.get(key):
                values[key] = loaded[key]

    def _warn_limited(self, key: str, message: str, *args: object) -> None:
        now = time.monotonic()
        if now < self._warning_deadlines.get(key, 0.0):
            return
        self._warning_deadlines[key] = now + 60.0
        self._logger.warning(message, *args)
