from __future__ import annotations

"""guard 运行路径派生规则。"""

from pathlib import Path
from typing import Any


DEFAULT_ROOT_DIR = "/home/x/.daemon"
DEFAULT_TMP_DIR = "/home/x/tmp"
DEFAULT_PM2_HOME = "/home/x/.pm2"
DEFAULT_ENV_FILE = "/home/x/.bashrc"


def runtime_root_dir(cfg: dict[str, Any] | None) -> str:
    runtime = ((cfg or {}).get("runtime", {}) or {}) if isinstance(cfg, dict) else {}
    return str(runtime.get("root_dir") or DEFAULT_ROOT_DIR).rstrip("/")


def runtime_tmp_dir(cfg: dict[str, Any] | None) -> str:
    runtime = ((cfg or {}).get("runtime", {}) or {}) if isinstance(cfg, dict) else {}
    value = str(runtime.get("tmp_dir") or "").strip()
    if value:
        return value.rstrip("/")
    root = Path(runtime_root_dir(cfg))
    return str(root.parent / "tmp")


def root_path(cfg: dict[str, Any] | None, *parts: str) -> str:
    return str(Path(runtime_root_dir(cfg), *parts))


def tmp_path(cfg: dict[str, Any] | None, *parts: str) -> str:
    return str(Path(runtime_tmp_dir(cfg), *parts))
