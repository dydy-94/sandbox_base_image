from __future__ import annotations

"""镜像内置 guard 的一次性激活辅助逻辑。"""

import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from .common import ensure_parent, log, now_iso, prepare_async_child_env, run_command, shlex_quote
from .paths import root_path, runtime_root_dir


def activation_pending_file(cfg: dict[str, Any] | None = None) -> str:
    return root_path(cfg, "events", "activation_pending.json")


def runtime_config_path(cfg: dict[str, Any] | None = None, cfg_path: str | None = None) -> str:
    if cfg_path:
        return str(Path(cfg_path).expanduser())
    return str(Path(runtime_root_dir(cfg), "config.json"))


def write_activation_pending(cfg: dict[str, Any] | None = None, *, reason: str = "config_missing") -> None:
    path = Path(activation_pending_file(cfg)).expanduser()
    payload = {"reason": str(reason), "source": "launcher", "created_at": now_iso()}
    try:
        ensure_parent(path)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        log("warn", "activation.pending.write_failed", "guard 激活 pending 标识写入失败", path=str(path), error=str(exc))


def consume_activation_pending(cfg: dict[str, Any] | None = None) -> bool:
    path = Path(activation_pending_file(cfg)).expanduser()
    if not path.exists():
        return False
    try:
        path.unlink()
    except Exception as exc:
        log("warn", "activation.pending.consume_failed", "guard 激活 pending 标识消费失败", path=str(path), error=str(exc))
        return False
    return True


def has_activation_pending(cfg: dict[str, Any] | None = None) -> bool:
    return Path(activation_pending_file(cfg)).expanduser().exists()


def schedule_bootstrap_entry(cfg: dict[str, Any], cfg_path: str, *, from_launcher: bool = False) -> bool:
    script = Path(runtime_root_dir(cfg)).expanduser() / "sandbox_guard.py"
    if not script.exists():
        script = Path(__file__).resolve().parent.parent.parent / "sandbox_guard.py"
    cmd = (
        f"{shlex_quote(sys.executable)} {shlex_quote(str(script))} "
        f"bootstrap-entry --config {shlex_quote(cfg_path)}"
    )
    if from_launcher:
        cmd += " --from-launcher"
    try:
        subprocess.Popen(cmd, shell=True, env=prepare_async_child_env())
        log("info", "activation.bootstrap_entry.scheduled", "已调度 guard bootstrap-entry", config=cfg_path, from_launcher=from_launcher)
        return True
    except Exception as exc:
        log("warn", "activation.bootstrap_entry.schedule_failed", "guard bootstrap-entry 调度失败", config=cfg_path, error=str(exc))
        return False


def start_launcher_via_supervisor(cfg: dict[str, Any]) -> bool:
    """启动镜像预置 launcher，确保 bootstrap 日志继承 Supervisor 标准输出。"""
    supervisor = ((cfg.get("runtime", {}) or {}).get("supervisor", {}) or {})
    ctl_bin = str(supervisor.get("ctl_bin") or "supervisorctl")
    ctl_conf = str(supervisor.get("ctl_conf") or "").strip()
    program = str(supervisor.get("launcher_program") or "sandbox-guard-launcher")
    base = f"{ctl_bin} -c {shlex_quote(ctl_conf)}" if ctl_conf else ctl_bin
    result = run_command(f"{base} start {shlex_quote(program)}", timeout=60)
    text = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode == 0 or "already started" in text.lower():
        log("info", "activation.launcher.supervisor_started", "已通过 Supervisor 启动 guard launcher", program=program)
        return True
    log(
        "warn",
        "activation.launcher.supervisor_start_failed",
        "通过 Supervisor 启动 guard launcher 失败",
        program=program,
        error=text[:500],
    )
    return False
