from __future__ import annotations

"""公共类型定义。"""

from dataclasses import dataclass
from typing import Any


@dataclass
class CommandResult:
    """外部命令执行结果。"""

    returncode: int
    stdout: str
    stderr: str


@dataclass
class ProbeResult:
    """进程探测结果（管理器无关）。"""

    exists: bool
    running: bool
    transitional: bool
    raw_status: str
    message: str
    details: dict[str, Any] | None = None
