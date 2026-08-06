from __future__ import annotations

"""不依赖外部进程管理器的直接进程策略。"""

import os
from pathlib import Path
import pwd
import signal
import subprocess
import time
from typing import Any

from ..common import ensure_parent, load_json, now_iso, write_json_atomic
from ..env_store import read_env_cache
from ..types import CommandResult, ProbeResult
from .base import ProcessManagerStrategy


class DirectStrategy(ProcessManagerStrategy):
    """以前台命令启动独立进程组，并通过 PID 身份文件管理生命周期。"""

    manager_name = "direct"

    def _options(self, proc: dict[str, Any]) -> dict[str, Any]:
        options = proc.get("manager_options", {}) or {}
        return options if isinstance(options, dict) else {}

    def _pid_file(self, proc: dict[str, Any], cfg: dict[str, Any]) -> Path:
        configured = str(self._options(proc).get("pid_file", "")).strip()
        if configured:
            return Path(configured)
        root_dir = Path(str((cfg.get("runtime", {}) or {}).get("root_dir", "/home/x/.daemon")))
        return root_dir / "pids" / f"{str(proc.get('name', 'process')).strip()}.json"

    def _boot_id(self) -> str:
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def _process_identity(self, pid: int) -> tuple[str, str] | None:
        """返回 Linux 进程状态和启动 ticks；僵尸进程视为不存在。"""
        try:
            text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            suffix = text[text.rfind(")") + 2 :].split()
            state = suffix[0]
            start_ticks = suffix[19]
            if state == "Z":
                return None
            return state, start_ticks
        except Exception:
            return None

    def _remove_pid_file(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    def _process_group_alive(self, pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _reap_child(self, pid: int) -> None:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, ProcessLookupError, OSError):
            pass

    def _owned_process(self, proc: dict[str, Any], cfg: dict[str, Any]) -> tuple[int, int] | None:
        path = self._pid_file(proc, cfg)
        record = load_json(str(path))
        try:
            pid = int(record.get("pid", 0))
            pgid = int(record.get("pgid", pid))
        except (TypeError, ValueError):
            pid = 0
            pgid = 0
        expected_ticks = str(record.get("process_start_ticks", "")).strip()
        expected_boot_id = str(record.get("boot_id", "")).strip()
        identity = self._process_identity(pid) if pid > 1 else None
        current_boot_id = self._boot_id()
        if (
            identity is None
            or not expected_ticks
            or identity[1] != expected_ticks
            or (expected_boot_id and current_boot_id and expected_boot_id != current_boot_id)
        ):
            self._remove_pid_file(path)
            return None
        try:
            if os.getpgid(pid) != pgid:
                self._remove_pid_file(path)
                return None
        except (ProcessLookupError, PermissionError, OSError):
            self._remove_pid_file(path)
            return None
        return pid, pgid

    def probe(self, proc: dict[str, Any], cfg: dict[str, Any]) -> ProbeResult:
        owned = self._owned_process(proc, cfg)
        if owned is None:
            return ProbeResult(False, False, False, "NOT_FOUND", "direct process not found")
        pid, pgid = owned
        return ProbeResult(
            True,
            True,
            False,
            "RUNNING",
            "direct process is running",
            {"pid": pid, "pgid": pgid},
        )

    def _child_env(self, cfg: dict[str, Any]) -> dict[str, str]:
        env = dict(os.environ)
        env.update({str(key): str(value) for key, value in read_env_cache(cfg).items() if str(value)})
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    def _run_as_kwargs(self, proc: dict[str, Any]) -> tuple[dict[str, Any], str]:
        run_as = str(self._options(proc).get("run_as", "")).strip()
        if not run_as:
            return {}, ""
        try:
            account = pwd.getpwnam(run_as)
        except KeyError:
            return {}, f"run_as user not found: {run_as}"
        if os.geteuid() == account.pw_uid:
            return {}, ""
        if os.geteuid() != 0:
            return {}, f"cannot switch from uid {os.geteuid()} to user {run_as}"
        groups = os.getgrouplist(account.pw_name, account.pw_gid)
        return {
            "user": account.pw_uid,
            "group": account.pw_gid,
            "extra_groups": groups,
        }, ""

    def _open_output(self, path_value: str):
        if path_value == "inherit":
            return None
        path = Path(path_value)
        if not str(path).startswith("/proc/"):
            ensure_parent(path)
        return open(path, "ab", buffering=0)

    def start(self, proc: dict[str, Any], cfg: dict[str, Any]) -> CommandResult:
        if self._owned_process(proc, cfg) is not None:
            return CommandResult(0, "direct process already running", "")
        command = str(proc.get("start_command", "")).strip()
        if not command:
            return CommandResult(2, "", "start_command is empty")

        options = self._options(proc)
        # NOTE: defaulting to /proc/1/fd/1 causes "Permission denied" when
        # this strategy runs under user=x (rootless profile). Fall back to
        # /var/log/gem/<program>.log so the file is owned by user=x and
        # child processes can write to it. Callers can still override with
        # an explicit stdout_file option.
        prog_name = str(proc.get("program", "direct")).strip() or "direct"
        default_stdout = f"/var/log/gem/{prog_name}.log"
        default_stderr = f"/var/log/gem/{prog_name}_err.log"
        stdout_path = str(options.get("stdout_file", default_stdout)).strip() or default_stdout
        stderr_path = str(options.get("stderr_file", default_stderr)).strip() or default_stderr
        working_dir = str(options.get("working_dir", "")).strip() or None
        run_as_kwargs, run_as_error = self._run_as_kwargs(proc)
        if run_as_error:
            return CommandResult(2, "", run_as_error)

        stdout_fp = None
        stderr_fp = None
        child = None
        try:
            stdout_fp = self._open_output(stdout_path)
            stderr_fp = self._open_output(stderr_path)
            child = subprocess.Popen(
                ["/bin/sh", "-c", command],
                stdin=subprocess.DEVNULL,
                stdout=stdout_fp if stdout_path != "inherit" else None,
                stderr=stderr_fp if stderr_path != "inherit" else None,
                cwd=working_dir,
                env=self._child_env(cfg),
                start_new_session=True,
                **run_as_kwargs,
            )
        except Exception as exc:
            return CommandResult(1, "", f"direct process start failed: {exc}")
        finally:
            if stdout_fp is not None:
                stdout_fp.close()
            if stderr_fp is not None:
                stderr_fp.close()

        identity = None
        for _ in range(10):
            if child.poll() is not None:
                return CommandResult(child.returncode or 1, "", "direct process exited during startup")
            identity = self._process_identity(child.pid)
            if identity is not None:
                break
            time.sleep(0.01)
        if identity is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except Exception:
                pass
            return CommandResult(1, "", "cannot identify direct process after startup")

        path = self._pid_file(proc, cfg)
        try:
            write_json_atomic(
                str(path),
                {
                    "pid": child.pid,
                    "pgid": child.pid,
                    "boot_id": self._boot_id(),
                    "process_start_ticks": identity[1],
                    "command": command,
                    "started_at": now_iso(),
                },
            )
        except Exception as exc:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except Exception:
                pass
            return CommandResult(1, "", f"direct process pid file write failed: {exc}")
        return CommandResult(0, f"direct process started pid={child.pid}", "")

    def start_with_timeout(self, proc: dict[str, Any], cfg: dict[str, Any], timeout_seconds: float) -> CommandResult:
        return self.start(proc, cfg)

    def stop(self, proc: dict[str, Any], cfg: dict[str, Any]) -> CommandResult:
        owned = self._owned_process(proc, cfg)
        if owned is None:
            return CommandResult(0, "direct process already stopped", "")
        pid, pgid = owned
        timeout_seconds = max(0.1, float(self._options(proc).get("stop_timeout_seconds", 5)))
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            self._remove_pid_file(self._pid_file(proc, cfg))
            return CommandResult(0, "direct process already stopped", "")
        except (PermissionError, OSError) as exc:
            return CommandResult(1, "", f"direct process SIGTERM failed: {exc}")

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self._reap_child(pid)
            if not self._process_group_alive(pgid):
                self._remove_pid_file(self._pid_file(proc, cfg))
                return CommandResult(0, f"direct process stopped pid={pid}", "")
            time.sleep(0.05)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except (PermissionError, OSError) as exc:
            return CommandResult(1, "", f"direct process SIGKILL failed: {exc}")
        for _ in range(20):
            self._reap_child(pid)
            if not self._process_group_alive(pgid):
                self._remove_pid_file(self._pid_file(proc, cfg))
                return CommandResult(0, f"direct process killed pid={pid}", "")
            time.sleep(0.05)
        return CommandResult(1, "", f"direct process did not exit pid={pid}")

    def restart(self, proc: dict[str, Any], cfg: dict[str, Any]) -> CommandResult:
        stopped = self.stop(proc, cfg)
        if stopped.returncode != 0:
            return stopped
        return self.start(proc, cfg)
