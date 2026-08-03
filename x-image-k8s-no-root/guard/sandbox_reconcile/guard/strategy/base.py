from __future__ import annotations

"""策略接口定义。"""

from typing import Any

from ..types import CommandResult, ProbeResult


class ProcessManagerStrategy:
    """进程管理器策略抽象基类。"""

    manager_name = "base"

    def probe(self, proc: dict[str, Any], cfg: dict[str, Any]) -> ProbeResult:
        raise NotImplementedError

    def start(self, proc: dict[str, Any], cfg: dict[str, Any]) -> CommandResult:
        raise NotImplementedError

    def start_with_timeout(self, proc: dict[str, Any], cfg: dict[str, Any], timeout_seconds: float) -> CommandResult:
        """带超时约束的启动。

        默认回退到普通 start，实现最小兼容；
        仅在特定 manager 需要特殊超时策略时再覆盖。
        """
        return self.start(proc, cfg)

    def restart(self, proc: dict[str, Any], cfg: dict[str, Any]) -> CommandResult:
        raise NotImplementedError

    def stop(self, proc: dict[str, Any], cfg: dict[str, Any]) -> CommandResult:
        """停止进程但不删除安装文件。"""
        raise NotImplementedError
