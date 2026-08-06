from __future__ import annotations

"""Container-aware resource usage collection.

All probes are implemented as direct file/stat reads. CPU usage is derived from
two daemon-cycle samples of cumulative cgroup CPU time, so collection does not
sleep or invoke external commands.
"""

import shutil
import time
from pathlib import Path
from typing import Any

_UNLIMITED_CGROUP_V1 = 9_000_000_000_000_000_000


def current_disk_used_percent() -> float:
    usage = shutil.disk_usage("/")
    if usage.total <= 0:
        return 0.0
    return round((usage.used / usage.total) * 100, 2)


def current_disk_free_bytes() -> int:
    return int(shutil.disk_usage("/").free)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _read_int(path: Path) -> int | None:
    text = _read_text(path)
    if not text:
        return None
    try:
        return int(text.split()[0])
    except Exception:
        return None


def _read_stat(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    text = _read_text(path)
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            out[parts[0]] = int(parts[1])
        except Exception:
            continue
    return out


def _is_cgroup_v2(root: Path) -> bool:
    return (root / "cgroup.controllers").exists()


def _bounded_limit(value: int | None) -> int | None:
    if value is None or value <= 0 or value >= _UNLIMITED_CGROUP_V1:
        return None
    return value


def _memory_usage(root: Path) -> dict[str, Any]:
    if _is_cgroup_v2(root):
        used = _read_int(root / "memory.current")
        raw_limit = _read_text(root / "memory.max")
        limit = None
        if raw_limit and raw_limit != "max":
            try:
                limit = _bounded_limit(int(raw_limit.split()[0]))
            except Exception:
                limit = None
    else:
        used = _read_int(root / "memory" / "memory.usage_in_bytes")
        stat = _read_stat(root / "memory" / "memory.stat")
        limit = _bounded_limit(stat.get("hierarchical_memory_limit"))
        if limit is None:
            limit = _bounded_limit(_read_int(root / "memory" / "memory.limit_in_bytes"))

    percent = None
    if used is not None and limit:
        percent = round((used / limit) * 100, 2)
    return {
        "memoryUsedBytes": used,
        "memoryLimitBytes": limit,
        "memoryUsedPercent": percent,
    }


def _parse_cpuset_count(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    total = 0
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start, end = part.split("-", 1)
                total += int(end) - int(start) + 1
            else:
                int(part)
                total += 1
        except Exception:
            return None
    return total if total > 0 else None


def _cpuset_cores(root: Path) -> float | None:
    for path in (
        root / "cpuset.cpus.effective",
        root / "cpuset.cpus",
        root / "cpuset" / "cpuset.cpus",
    ):
        count = _parse_cpuset_count(_read_text(path))
        if count:
            return float(count)
    return None


def _cpu_usage_ns(root: Path) -> int | None:
    if _is_cgroup_v2(root):
        stat = _read_stat(root / "cpu.stat")
        usage_usec = stat.get("usage_usec")
        return usage_usec * 1000 if usage_usec is not None else None
    return _read_int(root / "cpuacct" / "cpuacct.usage")


def _cpu_quota_cores(root: Path) -> float | None:
    if _is_cgroup_v2(root):
        raw = _read_text(root / "cpu.max")
        parts = raw.split()
        if len(parts) < 2 or parts[0] == "max":
            return None
        try:
            quota = int(parts[0])
            period = int(parts[1])
        except Exception:
            return None
    else:
        quota = _read_int(root / "cpu" / "cpu.cfs_quota_us")
        period = _read_int(root / "cpu" / "cpu.cfs_period_us")
        if quota is None or quota < 0:
            return None
    if not period or period <= 0:
        return None
    return quota / period


def _cpu_limit_cores(root: Path) -> float | None:
    quota = _cpu_quota_cores(root)
    cpuset = _cpuset_cores(root)
    values = [value for value in (quota, cpuset) if value is not None and value > 0]
    if not values:
        return None
    return round(min(values), 4)


def _cpu_usage(root: Path, prev_resources: dict[str, Any], now_ms: int) -> dict[str, Any]:
    usage_ns = _cpu_usage_ns(root)
    limit_cores = _cpu_limit_cores(root)
    sample = {"usageNs": usage_ns, "sampledAtMs": now_ms} if usage_ns is not None else None
    out: dict[str, Any] = {
        "cpuUsedCores": None,
        "cpuLimitCores": limit_cores,
        "cpuUsedPercent": None,
        "cpuSample": sample,
    }
    if usage_ns is None:
        return out

    prev_sample = prev_resources.get("cpuSample") if isinstance(prev_resources, dict) else None
    if not isinstance(prev_sample, dict):
        return out
    try:
        prev_usage = int(prev_sample.get("usageNs"))
        prev_ms = int(prev_sample.get("sampledAtMs"))
    except Exception:
        return out
    delta_usage = usage_ns - prev_usage
    delta_seconds = (now_ms - prev_ms) / 1000.0
    if delta_usage < 0 or delta_seconds <= 0:
        return out

    used_cores = delta_usage / (delta_seconds * 1_000_000_000)
    out["cpuUsedCores"] = round(max(0.0, used_cores), 4)
    if limit_cores and limit_cores > 0:
        out["cpuUsedPercent"] = round((used_cores / limit_cores) * 100, 2)
    return out


def collect_resource_usage(prev_resources: dict[str, Any] | None = None, cgroup_root: str | Path = "/sys/fs/cgroup") -> dict[str, Any]:
    root = Path(cgroup_root)
    now_ms = int(time.time() * 1000)
    resources: dict[str, Any] = {
        "diskUsedPercent": current_disk_used_percent(),
    }
    resources.update(_memory_usage(root))
    resources.update(_cpu_usage(root, prev_resources or {}, now_ms))
    return resources
