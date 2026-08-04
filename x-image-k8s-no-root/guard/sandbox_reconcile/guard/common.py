from __future__ import annotations

"""通用工具。"""

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any
from zoneinfo import ZoneInfo

from .types import CommandResult

_PID1_STDOUT = Path("/proc/1/fd/1")
_PID1_STDERR = Path("/proc/1/fd/2")
APP_NAME = "sandbox_guard"
LOG_TIMEZONE = ZoneInfo("Asia/Shanghai")
MIRROR_PID1_ENV = "SANDBOX_GUARD_MIRROR_PID1"
LOG_TO_PID1_ONLY_ENV = "SANDBOX_GUARD_LOG_TO_PID1_ONLY"
BOOTSTRAP_SOURCE_ENV = "SANDBOX_GUARD_BOOTSTRAP_SOURCE"
SANDBOX_ID_ENV = "X_SANDBOX_ID"


def now_iso() -> str:
    """返回 UTC ISO8601 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _log_timestamp() -> str:
    """返回日志时间。"""
    return datetime.now(LOG_TIMEZONE).isoformat(timespec="milliseconds")


def _log_identifier() -> str:
    """构造统一标识。"""
    sandbox_id = os.environ.get("DAYTONA_SANDBOX_ID", "").strip() or os.environ.get(SANDBOX_ID_ENV, "").strip() or "-"
    user_id = os.environ.get("X_SANDBOX_USER_ID", "").strip() or "-"
    user_name = os.environ.get("X_SANDBOX_USER_NAME", "").strip() or "-"
    return f"sandbox_id {sandbox_id}, user {user_id} ({user_name})"


def log(level: str, event: str, message: str, **kwargs: Any) -> None:
    """输出单行 JSON 日志到标准输出/错误。"""
    payload = {
        "ts": _log_timestamp(),
        "level": level,
        "app": APP_NAME,
        "event": event,
        "identifier": _log_identifier(),
        "msg": message,
    }
    if kwargs:
        payload["data"] = kwargs
    stream = os.sys.stderr if level in {"error", "warn"} else os.sys.stdout
    line = json.dumps(payload, ensure_ascii=False)
    pid1_only = os.environ.get(LOG_TO_PID1_ONLY_ENV, "").strip() == "1"
    if not pid1_only:
        try:
            print(line, file=stream, flush=True)
        except (BrokenPipeError, OSError):
            pass
    mirrored = False
    # 仅在显式开启时镜像到容器 PID1，避免 daemon 在 supervisor 下重复日志。
    if os.environ.get(MIRROR_PID1_ENV, "").strip() == "1":
        target = _PID1_STDERR if level in {"error", "warn"} else _PID1_STDOUT
        try:
            with open(target, "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
            mirrored = True
        except Exception:
            pass
    if pid1_only and not mirrored:
        try:
            print(line, file=stream, flush=True)
        except (BrokenPipeError, OSError):
            # 日志通道失效不能中断升级、事件落盘或进程守护。
            pass


def prepare_async_child_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """为 bootstrap 拉起的异步 guard 子进程准备日志环境。"""
    child_env = dict(os.environ if env is None else env)
    if child_env.get(BOOTSTRAP_SOURCE_ENV, "").strip() == "launcher":
        child_env[MIRROR_PID1_ENV] = "1"
        child_env[LOG_TO_PID1_ONLY_ENV] = "1"
    return child_env


def run_command(cmd: str, timeout: int = 30, env: dict[str, str] | None = None) -> CommandResult:
    """执行 shell 命令并返回标准化结果。"""
    p = subprocess.run(
        cmd,
        shell=True,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    return CommandResult(returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)


def ensure_parent(path: str | Path) -> None:
    """确保目标文件父目录存在。"""
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def write_json_atomic(path: str, payload: dict[str, Any]) -> None:
    """原子写 JSON，避免状态文件半写入损坏。"""
    ensure_parent(path)
    target = Path(path).expanduser().resolve()
    tmp = target.parent / f".{target.name}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)


def load_json(path: str) -> dict[str, Any]:
    """读取 JSON；不存在或损坏时返回空对象。"""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def file_hash(path: str | Path) -> str:
    """计算文件 sha256。"""
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class FileLock:
    """非阻塞文件锁，用于保证单实例执行。"""

    def __init__(self, path: str, *, inherit_on_exec: bool = False):
        self.path = path
        self.inherit_on_exec = inherit_on_exec
        self.fp = None

    def __enter__(self):
        ensure_parent(self.path)
        self.fp = open(self.path, "w", encoding="utf-8")
        os.set_inheritable(self.fp.fileno(), self.inherit_on_exec)
        try:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.fp.close()
            self.fp = None
            raise
        self.fp.write(str(os.getpid()))
        self.fp.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fp:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
            self.fp.close()
            self.fp = None
        return False


def shlex_quote(value: str) -> str:
    """最小化 shell 参数转义。"""
    return "'" + value.replace("'", "'\"'\"'") + "'"
