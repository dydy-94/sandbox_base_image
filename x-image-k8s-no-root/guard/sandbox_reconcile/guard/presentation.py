from __future__ import annotations

"""日志和状态展示相关的辅助函数。

这里集中放：
1. 状态到中文文案的映射；
2. daemon 汇总日志的拼接；
3. bootstrap 脚本文案的统一生成。
"""

import shutil
from typing import Any

from .resources import current_disk_used_percent


SANDBOX_STATUS_TEXT = {
    "ready": "已就绪",
    "starting": "启动中",
    "degraded": "部分异常",
    "upgrading": "升级中",
    "failed": "失败",
}


def human_bytes(num_bytes: int) -> str:
    """将字节数格式化为简短字符串。"""
    units = ["B", "K", "M", "G", "T", "P"]
    value = float(max(0, num_bytes))
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024.0
    if unit == "B":
        return f"{int(value)}{unit}"
    if value >= 10:
        return f"{value:.0f}{unit}"
    return f"{value:.1f}{unit}"


def sandbox_status_text(status: str) -> str:
    """返回沙箱状态的中文描述。"""
    return SANDBOX_STATUS_TEXT.get(str(status), str(status))


def current_disk_usage_text() -> str:
    """返回根文件系统磁盘占用文案。"""
    try:
        usage = shutil.disk_usage("/")
        disk_percent = int(current_disk_used_percent())
        return f"{human_bytes(usage.used)}/{human_bytes(usage.total)}({disk_percent}%)"
    except Exception:
        return "未知"


def _percent_text(value: Any) -> str:
    try:
        return f"{float(value):.2f}%"
    except Exception:
        return "未知"


def daemon_cycle_summary(state: dict[str, Any]) -> tuple[str, str]:
    """生成每轮 daemon 扫描汇总日志。"""
    summary = state.get("summary", {}) or {}
    processes = state.get("processes", {}) or {}
    handled_processes: list[str] = []
    unrecovered_processes: list[str] = []
    unknown_processes: list[str] = []
    for name, rec in processes.items():
        if not isinstance(rec, dict):
            continue
        action = str(rec.get("last_action", "none"))
        action_result = str(rec.get("last_action_result", "skipped"))
        status = str(rec.get("status", ""))
        manager_raw_status = str(rec.get("manager_raw_status", "")).strip().upper()
        if action in {"start", "restart", "delete", "delete_duplicates"} and action_result == "success":
            handled_processes.append(str(name))
        if manager_raw_status == "ERROR":
            unknown_processes.append(str(name))
        elif status in {"recovering", "failed", "upgrading"}:
            unrecovered_processes.append(str(name))

    overall_status = str(state.get("overall_status", "ok"))
    level = "info"
    if overall_status == "failed" or state.get("errors"):
        level = "error"
    elif overall_status == "degraded":
        level = "warn"

    errors = state.get("errors", []) or []
    process_reconcile_incomplete = any(
        str(error).strip().startswith("processes:") for error in errors
    )
    process_online = int(summary.get("process_online", 0) or 0)
    process_total = int(summary.get("process_total", 0) or 0)
    if process_reconcile_incomplete:
        process_status = "进程巡检：未完成，"
    elif unknown_processes and len(unknown_processes) >= process_total > 0:
        process_status = (
            f"进程状态：巡检未知（{len(unknown_processes)}/{process_total}），"
            f"巡检未知进程：{','.join(unknown_processes)}，"
        )
    elif unknown_processes:
        process_status = (
            f"进程在线：{process_online}/{process_total}，"
            f"巡检未知进程：{','.join(unknown_processes)}，"
        )
    else:
        process_status = f"进程在线：{process_online}/{process_total}，"

    msg = (
        "daemon扫描完成，"
        f"沙箱状态：{sandbox_status_text(str(state.get('sandbox_status', 'unknown')))}，"
        f"{process_status}"
        f"处理进程：{','.join(handled_processes) or '无'}，"
        f"未恢复进程：{','.join(unrecovered_processes) or '无'}，"
        f"磁盘使用率：{current_disk_usage_text()}，"
        f"CPU使用率：{_percent_text((state.get('resources', {}) or {}).get('cpuUsedPercent'))}，"
        f"内存使用率：{_percent_text((state.get('resources', {}) or {}).get('memoryUsedPercent'))}"
    )
    return level, msg


def bootstrap_script_start_message(name: str, async_mode: bool = False) -> str:
    if async_mode:
        return "脚本开始执行（异步）"
    return "脚本开始执行"


def bootstrap_script_output_message(name: str) -> str:
    return "脚本输出"


def bootstrap_script_exit_message(name: str) -> str:
    return "脚本执行结束"
