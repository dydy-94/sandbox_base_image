from __future__ import annotations

"""策略工厂。"""

from .base import ProcessManagerStrategy
from .direct import DirectStrategy
from .pm2 import PM2Strategy
from .supervisor import SupervisorStrategy


def get_strategy(name: str) -> ProcessManagerStrategy:
    """根据 manager 字段返回对应策略实例。"""
    if name == "pm2":
        return PM2Strategy()
    if name == "supervisor":
        return SupervisorStrategy()
    if name == "direct":
        return DirectStrategy()
    raise ValueError(f"不支持的 manager: {name}")
