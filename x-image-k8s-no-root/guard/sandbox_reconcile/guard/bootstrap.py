from __future__ import annotations

"""bootstrap 执行逻辑。"""

import uuid
import sys
import time
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from .common import load_json, log, now_iso, prepare_async_child_env, run_command, shlex_quote, write_json_atomic
from .paths import root_path, runtime_root_dir
from .presentation import (
    bootstrap_script_exit_message,
    bootstrap_script_output_message,
    bootstrap_script_start_message,
)
from .reconcile.env import reconcile_env
from .reconcile.processes import bootstrap_processes
from .resources import collect_resource_usage
from .runtime_profile import is_rootless_profile
from .runtime_permissions import ensure_rootless_runtime_dirs
from .skills import append_bootstrap_skill_request
from .state import build_base_state, finalize_overall_status
from .strategy.pm2 import cleanup_rootless_pm2_anchor
from .xagent_activity_notify import initialize_xagent_activity_notify


def _start_daemon(cfg: dict[str, Any]) -> None:
    """通过 supervisor 重载并启动/重启 daemon。"""
    runtime_sup = (cfg.get("runtime", {}) or {}).get("supervisor", {}) or {}
    ctl_bin = str(runtime_sup["ctl_bin"])
    ctl_conf = runtime_sup.get("ctl_conf")
    program = str(runtime_sup.get("daemon_program", "sandbox-daemon"))
    conf_dir = Path(str(runtime_sup.get("conf_dir", "/opt/gem/supervisord")))
    daemon_autostart = bool(runtime_sup.get("daemon_autostart", False))
    launcher_program = str(runtime_sup.get("launcher_program", "sandbox-guard-launcher"))
    runtime = cfg.get("runtime", {}) or {}
    root_dir = Path(runtime_root_dir(cfg))
    config_path = root_dir / "config.json"
    sandbox_id = str(os.environ.get("X_SANDBOX_ID", "")).replace('"', '\\"')
    sandbox_type = str(os.environ.get("X_SANDBOX_TYPE", "")).replace('"', '\\"')
    sandbox_platform = str(os.environ.get("X_SANDBOX_PLATFORM", "")).replace('"', '\\"')
    user_id = str(os.environ.get("X_SANDBOX_USER_ID", "")).replace('"', '\\"')
    user_name = str(os.environ.get("X_SANDBOX_USER_NAME", "")).replace('"', '\\"')
    env_line = (
        f'PYTHONUNBUFFERED="1",X_SANDBOX_ID="{sandbox_id}",X_SANDBOX_TYPE="{sandbox_type}",'
        f'X_SANDBOX_PLATFORM="{sandbox_platform}",X_SANDBOX_USER_ID="{user_id}",X_SANDBOX_USER_NAME="{user_name}"'
    )

    daemon_cmd = f"{shlex_quote(sys.executable)} -u {shlex_quote(str(root_dir / 'sandbox_guard.py'))} daemon --config {shlex_quote(str(config_path))}"
    daemon_conf = (
        f"[program:{program}]\n"
        f"command={daemon_cmd}\n"
        f"directory={root_dir}\n"
        f"autostart={'true' if daemon_autostart else 'false'}\n"
        "autorestart=true\n"
        "startsecs=1\n"
        "startretries=3\n"
        "stopsignal=TERM\n"
        "stopasgroup=true\n"
        "killasgroup=true\n"
        f"environment={env_line}\n"
        "stdout_logfile=/proc/1/fd/1\n"
        "stdout_logfile_maxbytes=0\n"
        "stderr_logfile=/proc/1/fd/2\n"
        "stderr_logfile_maxbytes=0\n"
    )
    launcher_cmd = (
        f"{shlex_quote(sys.executable)} -u {shlex_quote(str(root_dir / 'sandbox_guard.py'))} "
        f"launcher --config {shlex_quote(str(config_path))}"
    )
    launcher_conf = (
        f"[program:{launcher_program}]\n"
        f"command={launcher_cmd}\n"
        f"directory={root_dir}\n"
        "autostart=true\n"
        "autorestart=false\n"
        "startsecs=0\n"
        "startretries=1\n"
        "stdout_logfile=/proc/1/fd/1\n"
        "stdout_logfile_maxbytes=0\n"
        "stderr_logfile=/proc/1/fd/2\n"
        "stderr_logfile_maxbytes=0\n"
    )
    base = f"{ctl_bin} -c {shlex_quote(str(ctl_conf))}" if ctl_conf else ctl_bin
    manage_program_conf = bool(runtime_sup.get("manage_program_conf", not is_rootless_profile(cfg)))
    if manage_program_conf:
        try:
            conf_dir.mkdir(parents=True, exist_ok=True)
            (conf_dir / f"{program}.conf").write_text(daemon_conf, encoding="utf-8")
            (conf_dir / f"{launcher_program}.conf").write_text(launcher_conf, encoding="utf-8")
        except Exception as exc:
            log("error", "bootstrap.daemon.supervisor.write_conf_failed", "写 daemon supervisor 配置失败", program=program, error=str(exc))
            return

        reread = run_command(f"{base} reread", timeout=60)
        if reread.returncode != 0:
            log(
                "error",
                "bootstrap.daemon.supervisor.reread_failed",
                "supervisor reread 失败",
                program=program,
                stderr=(reread.stderr.strip() or reread.stdout.strip())[:200],
            )
            return
        update = run_command(f"{base} update {shlex_quote(program)}", timeout=60)
        if update.returncode != 0:
            log(
                "warn",
                "bootstrap.daemon.supervisor.update_failed",
                "supervisor update sandbox-daemon 失败，继续尝试 restart/start",
                program=program,
                stderr=(update.stderr.strip() or update.stdout.strip())[:200],
            )
    else:
        log(
            "info",
            "bootstrap.daemon.supervisor.preinstalled",
            "rootless 模式使用镜像预置 Supervisor 配置",
            program=program,
        )

    # 每次 bootstrap 后都优先重启 daemon，确保代码更新立即生效。
    restart = run_command(f"{base} restart {shlex_quote(program)}", timeout=60)
    text = f"{restart.stdout}\n{restart.stderr}".lower()
    if restart.returncode == 0:
        log("info", "bootstrap.daemon.supervisor.restarted", "daemon 已由 supervisor 重启", program=program)
        return

    # 若当前未运行或未加载，降级为 start。
    if "not running" in text or "no such process" in text:
        start = run_command(f"{base} start {shlex_quote(program)}", timeout=60)
        start_text = f"{start.stdout}\n{start.stderr}".lower()
        if start.returncode == 0 or "already started" in start_text:
            log("info", "bootstrap.daemon.supervisor.started", "daemon 已由 supervisor 启动", program=program)
            return
        log(
            "error",
            "bootstrap.daemon.supervisor.start_failed",
            "supervisor 启动 daemon 失败",
            program=program,
            stderr=(start.stderr.strip() or start.stdout.strip())[:200],
        )
        return

    log(
        "error",
        "bootstrap.daemon.supervisor.restart_failed",
        "supervisor 重启 daemon 失败",
        program=program,
        stderr=(restart.stderr.strip() or restart.stdout.strip())[:200],
    )


def _resolve_runtime_path(cfg: dict[str, Any], path: str) -> str:
    p = Path(path).expanduser()
    if p.is_absolute():
        return str(p)
    return root_path(cfg, path)


def _build_script_command(item: dict[str, Any], cfg: dict[str, Any]) -> str:
    """构建 bootstrap 脚本命令。"""
    command = str(item.get("command", "")).strip()
    if command:
        return command

    path = str(item.get("path", "")).strip()
    args = item.get("args", []) or []
    if not path:
        return ""
    resolved_path = _resolve_runtime_path(cfg, path)
    quoted_args = " ".join(shlex_quote(str(a)) for a in args)
    if resolved_path.endswith(".py"):
        return f"{shlex_quote(sys.executable)} {shlex_quote(resolved_path)} {quoted_args}".strip()
    if resolved_path.endswith(".sh"):
        return f"bash {shlex_quote(resolved_path)} {quoted_args}".strip()
    return f"{shlex_quote(resolved_path)} {quoted_args}".strip()


def _stream_script_output(stream, name: str, stream_name: str) -> None:
    """逐行转发脚本输出到统一日志。"""
    if stream is None:
        return
    try:
        for line in iter(stream.readline, ""):
            text = line.strip()
            if text:
                log(
                    "info",
                    "bootstrap.script.output",
                    bootstrap_script_output_message(name),
                    script=name,
                    stream=stream_name,
                    output=text[:4000],
                )
    finally:
        stream.close()


def run_bootstrap_script_runner(name: str, command: str, timeout: int) -> int:
    """独立进程执行单个 bootstrap 脚本并持续转发日志。"""
    log("info", "bootstrap.script.start", bootstrap_script_start_message(name, async_mode=True), script=name)
    start = time.time()
    proc = subprocess.Popen(
        command,
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ),
    )
    threads = [
        threading.Thread(
            target=_stream_script_output,
            args=(proc.stdout, name, "stdout"),
            daemon=True,
        ),
        threading.Thread(
            target=_stream_script_output,
            args=(proc.stderr, name, "stderr"),
            daemon=True,
        ),
    ]
    for t in threads:
        t.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = proc.wait()
        duration_seconds = round(time.time() - start, 3)
        log(
            "warn",
            "bootstrap.script.exit",
            bootstrap_script_exit_message(name),
            script=name,
            duration_seconds=duration_seconds,
            returncode=rc,
            timeout=True,
        )
        return rc
    for t in threads:
        t.join(timeout=1)
    duration_seconds = round(time.time() - start, 3)
    log(
        "info",
        "bootstrap.script.exit",
        bootstrap_script_exit_message(name),
        script=name,
        duration_seconds=duration_seconds,
        returncode=rc,
    )
    return rc


def _spawn_async_bootstrap_script(name: str, cmd: str, timeout: int, cfg: dict[str, Any]) -> None:
    """拉起独立进程异步执行 bootstrap 脚本。"""
    root_dir = Path(runtime_root_dir(cfg))
    runner_cmd = [
        sys.executable,
        "-u",
        str(root_dir / "sandbox_guard.py"),
        "bootstrap-script-runner",
        "--name",
        name,
        "--command",
        cmd,
        "--timeout",
        str(timeout),
    ]
    subprocess.Popen(
        runner_cmd,
        start_new_session=True,
        env=prepare_async_child_env(),
    )


def _run_bootstrap_scripts(cfg: dict[str, Any], state: dict[str, Any]) -> None:
    """执行 bootstrap 脚本：只负责拉起并透传日志，不将脚本返回码视为流程失败。"""
    scripts = (cfg.get("bootstrap", {}) or {}).get("scripts", []) or []
    results: list[dict[str, Any]] = []
    for idx, item in enumerate(scripts):
        if not isinstance(item, dict):
            state["errors"].append(f"bootstrap_scripts[{idx}]: 配置格式错误")
            results.append(
                {
                    "name": f"script_{idx}",
                    "ok": False,
                    "returncode": -1,
                    "duration_ms": 0,
                    "message": "invalid script item",
                }
            )
            continue

        name = str(item.get("name") or f"script_{idx}")
        cmd = _build_script_command(item, cfg)
        timeout = int(item.get("timeout_seconds", 300))
        is_async = bool(item.get("async", False))
        if not cmd:
            state["errors"].append(f"bootstrap_script:{name}: 缺少 command/path")
            results.append(
                {
                    "name": name,
                    "ok": False,
                    "returncode": -1,
                    "duration_ms": 0,
                    "message": "missing command/path",
                }
            )
            continue

        if is_async:
            _spawn_async_bootstrap_script(name, cmd, timeout, cfg)
            results.append(
                {
                    "name": name,
                    "returncode": None,
                    "duration_ms": 0,
                    "message": "started asynchronously",
                    "async": True,
                }
            )
            continue

        log("info", "bootstrap.script.start", bootstrap_script_start_message(name), script=name)

        start = time.time()
        res = run_command(cmd, timeout=timeout)
        duration_ms = int((time.time() - start) * 1000)
        duration_seconds = round(duration_ms / 1000, 3)
        stdout_text = (res.stdout or "").strip()
        stderr_text = (res.stderr or "").strip()
        if stdout_text:
            log("info", "bootstrap.script.output", bootstrap_script_output_message(name), script=name, stream="stdout", output=stdout_text[:4000])
        if stderr_text:
            log("info", "bootstrap.script.output", bootstrap_script_output_message(name), script=name, stream="stderr", output=stderr_text[:4000])

        results.append(
            {
                "name": name,
                "returncode": res.returncode,
                "duration_ms": duration_ms,
                "message": "completed",
            }
        )
        log(
            "info",
            "bootstrap.script.exit",
            bootstrap_script_exit_message(name),
            script=name,
            duration_seconds=duration_seconds,
            returncode=res.returncode,
        )

    state["bootstrap_scripts"] = results


def _append_bootstrap_skill_sync_request(cfg: dict[str, Any]) -> None:
    try:
        append_bootstrap_skill_request(cfg)
    except Exception as exc:
        log("warn", "skill.request.bootstrap_failed", "bootstrap 追加 skill 同步请求失败", error=str(exc))


def _prewarm_resource_sample(cfg: dict[str, Any], state: dict[str, Any], prev: dict[str, Any]) -> None:
    try:
        state["resources"] = collect_resource_usage({})
        if bool((cfg.get("report", {}) or {}).get("enabled", False)):
            report_state = dict(state.get("report", {}) or {})
            interval = int((cfg.get("report", {}) or {}).get("interval_seconds", 60))
            if interval >= 0:
                report_state["next_report_due_at_ms"] = int(time.time() * 1000) + interval * 1000
                state["report"] = report_state
        log("info", "bootstrap.resources.prewarm", "bootstrap 已完成资源采样预热")
    except Exception as exc:
        state["errors"].append(f"resource_prewarm: {exc}")
        log("warn", "bootstrap.resources.prewarm_failed", "bootstrap 资源采样预热失败", error=str(exc))


def _wait_for_rootless_xagent_runner(
    cfg: dict[str, Any],
    state: dict[str, Any],
    runner: subprocess.Popen[Any] | None,
) -> None:
    """保持一次性 launcher 存活，直到 rootless xagent 升级任务结束。"""
    if not is_rootless_profile(cfg) or runner is None:
        return
    started_at = time.monotonic()
    while True:
        try:
            returncode = runner.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            # 防止已有 daemon 将长升级误判为陈旧 bootstrap 后并发接管。
            state["last_cycle_time"] = now_iso()
            try:
                write_json_atomic(str(cfg["runtime"]["state_file"]), state)
            except Exception as exc:
                log(
                    "warn",
                    "bootstrap.xagent_runner.heartbeat_failed",
                    "等待 xagent upgrade-runner 时刷新 bootstrap 状态失败",
                    error=str(exc),
                )
    log(
        "info" if returncode == 0 else "warn",
        "bootstrap.xagent_runner.exit",
        "bootstrap 等待 xagent upgrade-runner 结束",
        returncode=returncode,
        duration_seconds=round(time.monotonic() - started_at, 3),
    )


def run_bootstrap(
    cfg: dict[str, Any],
    cfg_path: str,
) -> int:
    """执行 bootstrap。

    设计目标：
    - 优先进入 bootstrapping phase，防止 daemon 并发介入；
    - 允许拉取远端配置；
    - 做轻量收敛并尝试启动 daemon。
    """
    prev_mirror = os.environ.get("SANDBOX_GUARD_MIRROR_PID1")
    if prev_mirror is None:
        os.environ["SANDBOX_GUARD_MIRROR_PID1"] = "1"
    try:
        ensure_rootless_runtime_dirs(cfg)
        runtime = cfg["runtime"]
        state_file = runtime["state_file"]
        prev = load_json(state_file)
        epoch = str(uuid.uuid4())
        state = build_base_state(cfg, phase="bootstrapping", prev=prev)
        state["bootstrap_epoch"] = epoch
        initialize_xagent_activity_notify(cfg, state)
        write_json_atomic(state_file, state)

        reconcile_env(cfg, state, phase="bootstrap")
        env_state = state.get("env", {}) or {}
        env_log_data: dict[str, Any] = {}
        for key in ["injected_keys", "skipped_optional_keys", "missing_keys"]:
            value = env_state.get(key, [])
            if value:
                env_log_data[key] = value
        write_error = str(env_state.get("write_error", "") or "").strip()
        if write_error:
            env_log_data["write_error"] = write_error
        log(
            "info" if not write_error else "warn",
            "bootstrap.env.applied",
            "bootstrap 环境变量处理完成",
            **env_log_data,
        )
        _run_bootstrap_scripts(cfg, state)
        scheduled_runners = bootstrap_processes(cfg, cfg_path, state, prev, run_command)
        _prewarm_resource_sample(cfg, state, prev)
        write_json_atomic(state_file, state)

        _wait_for_rootless_xagent_runner(cfg, state, scheduled_runners.get("xagent"))
        cleanup_rootless_pm2_anchor(cfg, timeout_seconds=5)
        _start_daemon(cfg)
        _append_bootstrap_skill_sync_request(cfg)

        state["phase"] = "running"
        state["last_cycle_time"] = now_iso()
        finalize_overall_status(state)
        write_json_atomic(state_file, state)
        return 0
    finally:
        if prev_mirror is None:
            os.environ.pop("SANDBOX_GUARD_MIRROR_PID1", None)
        else:
            os.environ["SANDBOX_GUARD_MIRROR_PID1"] = prev_mirror
