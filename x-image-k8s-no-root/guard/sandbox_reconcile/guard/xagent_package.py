from __future__ import annotations

"""Shared xagent source/binary package and installation detection."""

from pathlib import Path
from typing import Any

XAGENT_MODE_SOURCE = "source"
XAGENT_MODE_BINARY = "binary"


def detect_staged_xagent_package(
    stage_dir: Path,
    package_path: Path,
    *,
    source_root: str,
    binary_name: str,
) -> tuple[str, Path | None]:
    """Detect the fixed xagent package shape using direct root paths only."""
    source_name = str(source_root).strip()
    if source_name:
        source_candidate = stage_dir / source_name
        if source_candidate.is_dir():
            return XAGENT_MODE_SOURCE, source_candidate

    binary_file_name = str(binary_name).strip()
    if binary_file_name:
        binary_candidate = stage_dir / binary_file_name
        if binary_candidate.is_file():
            return XAGENT_MODE_BINARY, binary_candidate

    # A raw, uncompressed XAgent binary remains supported without copying it
    # into stage merely for shape detection.
    if binary_file_name and package_path.is_file() and package_path.name == binary_file_name:
        return XAGENT_MODE_BINARY, package_path
    return "", None


def detect_installed_xagent_mode(upgrade_cfg: dict[str, Any]) -> str | None:
    """Detect the installed source/binary layout from configured target paths."""
    source_text = str(upgrade_cfg.get("source_deploy_dir", "")).strip()
    if source_text:
        source_dir = Path(source_text)
        if source_dir.is_dir():
            return XAGENT_MODE_SOURCE

    binary_text = str(upgrade_cfg.get("binary_target", "")).strip()
    if binary_text:
        binary_target = Path(binary_text)
        if binary_target.is_file():
            return XAGENT_MODE_BINARY
    return None
