from __future__ import annotations

"""环境变量巡检。

设计约定：
- bootstrap 阶段负责把托管环境变量注入当前进程并落到 env_file；
- daemon 阶段不再修改环境变量，只检查 env_file 中的托管区块是否仍然存在。
"""

import os
from pathlib import Path
import re
from typing import Any

from ..common import FileLock, log
from ..env_store import env_lock_file, read_env_cache, service_dynamic_keys


def _resolve_desired_value(item: dict[str, Any]) -> str | None:
    """从配置项解析期望注入值。"""
    if "value" in item and item.get("value") is not None:
        return str(item.get("value"))
    explicit_flag = str(item.get("explicit_flag_env", "")).strip()
    if explicit_flag and os.environ.get(explicit_flag, "").strip() == "":
        return None
    src = str(item.get("value_from_env", "")).strip()
    if src:
        val = os.environ.get(src)
        if val is not None and str(val) != "":
            return str(val)
    return None


def _upsert_managed_env_block(env_file: str, items: dict[str, str]) -> None:
    """将托管环境变量写入固定区块（不会追加膨胀）。"""
    if not items:
        return
    p = Path(env_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text("", encoding="utf-8")
    begin = "# >>> sandbox_guard managed env >>>"
    end = "# <<< sandbox_guard managed env <<<"

    lines = [begin]
    for k in sorted(items.keys()):
        lines.append(f"export {k}={items[k]!r}")
    lines.append(end)
    block = "\n".join(lines)

    def _normalize_edges(text: str) -> str:
        lines = text.splitlines()
        normalized: list[str] = []
        prev_blank = False
        for line in lines:
            blank = line.strip() == ""
            if blank and prev_blank:
                continue
            normalized.append(line.rstrip())
            prev_blank = blank
        return "\n".join(normalized).strip("\n")

    content = _normalize_edges(p.read_text(encoding="utf-8"))
    if begin in content and end in content:
        start = content.index(begin)
        finish = content.index(end) + len(end)
        prefix = content[:start].rstrip("\n")
        suffix = content[finish:].lstrip("\n")
        parts = [part for part in [prefix, block, suffix] if part]
        p.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    else:
        base = content.rstrip("\n")
        parts = [part for part in [base, block] if part]
        p.write_text("\n\n".join(parts) + "\n", encoding="utf-8")


def _env_values_in_file(env_file: str) -> dict[str, str]:
    """扫描整个 env 文件中已声明的变量和值。

    仅解析简单的 shell 赋值语句：
    - export KEY=VALUE
    - KEY=VALUE
    """
    p = Path(env_file)
    if not p.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key] = value
    return values


_SHELL_VARIABLE_PATTERN = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _expand_simple_shell_value(raw_value: str, values: dict[str, str]) -> str | None:
    """安全展开简单 shell 变量引用，不执行命令或其他 shell 语法。"""
    value = raw_value.strip()
    if not value:
        return ""
    single_quoted = len(value) >= 2 and value[0] == value[-1] == "'"
    double_quoted = len(value) >= 2 and value[0] == value[-1] == '"'
    if single_quoted or double_quoted:
        value = value[1:-1]
    if single_quoted:
        return value
    if any(marker in value for marker in ["$(", "`", "<(", ">("]):
        return None

    unresolved = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal unresolved
        key = match.group(1) or match.group(2) or ""
        if key not in values:
            unresolved = True
            return ""
        return values[key]

    expanded = _SHELL_VARIABLE_PATTERN.sub(_replace, value)
    if unresolved or "$" in expanded:
        return None
    return expanded


def expanded_path_from_env_file(env_file: str, base_env: dict[str, str]) -> str | None:
    """按声明顺序安全解析 env 文件中的 PATH。

    只支持简单赋值及 ``$VAR``/``${VAR}`` 展开。遇到命令替换、未知变量或
    其他复杂 shell 语法时，返回调用方已有 PATH，避免把字面量表达式注入 PM2。
    """
    p = Path(env_file)
    if not p.exists():
        return None
    values = dict(base_env)
    base_path = values.get("PATH", "")
    path_seen = False
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        expanded = _expand_simple_shell_value(raw_value, values)
        if expanded is None:
            if key == "PATH":
                return base_path
            continue
        values[key] = expanded
        if key == "PATH":
            path_seen = True
    return values.get("PATH", base_path) if path_seen else None


def _env_keys_in_file(env_file: str) -> set[str]:
    """扫描整个 env 文件中已声明的变量名。"""
    return set(_env_values_in_file(env_file).keys())


def build_managed_env_items(
    cfg: dict[str, Any],
    *,
    phase: str,
    env_cache: dict[str, str] | None = None,
    file_values: dict[str, str] | None = None,
) -> dict[str, str]:
    """构造应写入 managed block 的变量。

    service_dynamic 的优先级：
    - update-env/bootstrap：使用 env cache；
    - daemon：如果 .bashrc 已有值则保留已有值，否则用 env cache 修复缺失值。
    """
    managed: dict[str, str] = {}
    cache = env_cache if env_cache is not None else read_env_cache(cfg)
    existing = file_values or {}
    for item in ((cfg.get("env", {}) or {}).get("items", []) or []):
        if not isinstance(item, dict):
            continue
        apply_phase = item.get("apply_phase", "both")
        if phase not in {"update-env"} and apply_phase not in {"both", phase}:
            continue
        key = str(item.get("key", "")).strip()
        if not key:
            continue
        policy = str(item.get("policy", "required_present")).strip()
        if policy == "service_dynamic":
            if phase == "daemon" and key in existing:
                value = existing.get(key)
            else:
                value = cache.get(key)
            if value:
                managed[key] = str(value)
            continue
        desired = _resolve_desired_value(item)
        if desired is not None:
            managed[key] = desired
    return managed


def reconcile_env(cfg: dict[str, Any], state: dict[str, Any], phase: str) -> None:
    """执行 env 巡检。

    v1 策略：
    - 默认做存在性检查；
    - immutable 策略仅在声明 expected_value 时校验值一致性。
    """
    env_cfg = cfg.get("env", {}) or {}
    if not env_cfg.get("enabled", False):
        state["env"] = {"enabled": False, "status": "ok", "message": "disabled"}
        return

    missing: list[str] = []
    immutable_violations: list[str] = []
    injected_keys: list[str] = []
    skipped_optional_keys: list[str] = []
    managed_items: dict[str, str] = {}
    write_error = ""
    env_file = str((cfg.get("runtime", {}) or {}).get("env_file", "/home/x/.bashrc"))
    file_values = _env_values_in_file(env_file) if phase == "daemon" else {}
    declared_keys = set(file_values.keys()) if phase == "daemon" else set()
    env_cache = read_env_cache(cfg)
    service_keys = set(service_dynamic_keys(cfg))
    for item in env_cfg.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        apply_phase = item.get("apply_phase", "both")
        if apply_phase not in {"both", phase}:
            continue
        key = item.get("key")
        if not key:
            continue
        policy = str(item.get("policy", "required_present")).strip()
        if policy == "service_dynamic":
            desired = env_cache.get(str(key))
            if phase == "daemon":
                if str(key) in declared_keys:
                    continue
                if desired:
                    managed_items = build_managed_env_items(cfg, phase="daemon", env_cache=env_cache, file_values=file_values)
                    injected_keys.append(str(key))
                    continue
                skipped_optional_keys.append(str(key))
                continue
            if desired:
                os.environ[str(key)] = desired
                managed_items[str(key)] = desired
                injected_keys.append(str(key))
                continue
            skipped_optional_keys.append(str(key))
            continue
        explicit_flag = str(item.get("explicit_flag_env", "")).strip()
        if phase == "bootstrap" and policy == "optional_present" and explicit_flag and os.environ.get(explicit_flag, "").strip() == "":
            skipped_optional_keys.append(str(key))
            log("warn", "env.optional.skip", f"可选环境变量未传入，跳过注入：{key}", key=str(key))
            continue
        if phase == "daemon":
            val = str(key) if str(key) in declared_keys else None
        else:
            current = os.environ.get(key)
            val = current if current not in {None, ""} else None
        desired = _resolve_desired_value(item)
        if phase == "bootstrap" and desired is not None:
            # 仅 bootstrap 阶段写入托管区块，daemon 阶段只校验不补值。
            managed_items[str(key)] = desired
        if val is None:
            if desired is None or phase != "bootstrap":
                if policy == "optional_present":
                    if phase == "bootstrap":
                        skipped_optional_keys.append(str(key))
                        log("warn", "env.optional.skip", f"可选环境变量未传入，跳过注入：{key}", key=str(key))
                    continue
                missing.append(key)
                continue
            os.environ[str(key)] = desired
            val = desired
            injected_keys.append(str(key))
        if policy == "immutable":
            expected = item.get("expected_value")
            if expected is not None and str(expected) != val:
                immutable_violations.append(key)
    if managed_items:
        try:
            with FileLock(env_lock_file(cfg)):
                if phase == "daemon":
                    file_values = _env_values_in_file(env_file)
                    for key in service_keys:
                        if key in file_values:
                            managed_items[key] = file_values[key]
                _upsert_managed_env_block(env_file, managed_items)
        except Exception as exc:
            write_error = str(exc)

    status = "ok" if not missing and not immutable_violations and not write_error else "degraded"
    state["env"] = {
        "enabled": True,
        "status": status,
        "missing_keys": missing,
        "immutable_violations": immutable_violations,
        "injected_keys": injected_keys,
        "skipped_optional_keys": skipped_optional_keys,
        "env_file": env_file,
        "write_error": write_error,
        "message": "env checked",
    }
