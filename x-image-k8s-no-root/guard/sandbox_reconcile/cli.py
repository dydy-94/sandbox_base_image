from __future__ import annotations

"""命令行薄入口。

工程化原则：
1. CLI 只做参数解析与流程分发；
2. 具体业务逻辑下沉到 guard 子包；
3. 避免再次演化成超大单文件。
"""

from argparse import ArgumentParser
import json
import sys

from .guard.bootstrap import run_bootstrap, run_bootstrap_script_runner
from .guard.config import apply_defaults, read_config, validate_config
from .guard.constants import APP_VERSION
from .guard.daemon import run_daemon_loop
from .guard.entry import run_bootstrap_entry, run_launcher
from .guard.env_update import run_update_env
from .guard.paths import DEFAULT_ROOT_DIR
from .guard.process_applicability import process_applicability
from .guard.readiness import check_process_readiness
from .guard.redis_client import build_redis_client
from .guard.report import REPORT_SANDBOX_STATUS, append_report_request
from .guard.self_update import run_self_update
from .guard.skills import append_skill_request, run_skill_sync_runner, sandbox_type_for_skills, skill_sync_applicable
from .guard.upgrade import append_upgrade_request, run_upgrade_runner
from .guard.xagent_status import XAGENT_STATUS_READY, get_xagent_status

DEFAULT_CONFIG_PATH = f"{DEFAULT_ROOT_DIR}/config.json"


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Sandbox Guard")
    parser.add_argument("--version", action="store_true", help="show version")
    sub = parser.add_subparsers(dest="command")

    validate = sub.add_parser("validate", help="validate config")
    validate.add_argument("--config", default=DEFAULT_CONFIG_PATH)

    bootstrap = sub.add_parser("bootstrap", help="run bootstrap")
    bootstrap.add_argument("--config", default=DEFAULT_CONFIG_PATH)

    bootstrap_entry = sub.add_parser("bootstrap-entry", help="run bootstrap entry")
    bootstrap_entry.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    bootstrap_entry.add_argument("--sandbox-started-at-ms", default="")
    bootstrap_entry.add_argument("--user-id", default="")
    bootstrap_entry.add_argument("--user-name", default="")
    bootstrap_entry.add_argument("--base-url", default="")
    bootstrap_entry.add_argument("--auth-token", default="")
    bootstrap_entry.add_argument("--target-version", default="auto")
    bootstrap_entry.add_argument("--from-launcher", action="store_true")

    launcher = sub.add_parser("launcher", help="run image-bundled guard launcher")
    launcher.add_argument("--config", default=DEFAULT_CONFIG_PATH)

    update_env = sub.add_parser("update-env", help="update service dynamic environment")
    update_env.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    update_env.add_argument("--sandbox-id", default=None)
    update_env.add_argument("--sandbox-type", default=None)
    update_env.add_argument("--sandbox-platform", default=None)
    update_env.add_argument("--expert-enable-ha", choices=["true", "false"], default=None)
    update_env.add_argument("--user-id", default=None)
    update_env.add_argument("--user-name", default=None)
    update_env.add_argument("--base-url", default=None)
    update_env.add_argument("--auth-token", default=None)
    update_env.add_argument("--startup-type", default=None)
    update_env.add_argument("--sandbox-startup-duration-ms", default=None)
    update_env.add_argument("--sandbox-init-duration-ms", default=None)

    daemon = sub.add_parser("daemon", help="run daemon")
    daemon.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    daemon.add_argument("--once", action="store_true")
    daemon.add_argument("--interval", type=int)

    script_runner = sub.add_parser("bootstrap-script-runner", help="run one bootstrap script")
    script_runner.add_argument("--name", required=True)
    script_runner.add_argument("--command", dest="script_command", required=True)
    script_runner.add_argument("--timeout", type=int, default=300)

    runner = sub.add_parser("upgrade-runner", help="run async upgrade task")
    runner.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    runner.add_argument("--process", required=True)
    runner.add_argument("--target-version", required=True)

    version_cmd = sub.add_parser("version", help="show daemon version")

    readiness = sub.add_parser("readiness", help="check sandbox readiness")
    readiness.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    readiness.add_argument("--process", default="xagent")

    xagent_status = sub.add_parser("xagent-status", help="show xagent startup status")
    xagent_status.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    xagent_status.add_argument("--max-age-seconds", type=int, default=600)

    self_update = sub.add_parser("self-update", help="update daemon bundle and restart daemon")
    self_update.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    self_update.add_argument("--target-version", default="auto")

    force = sub.add_parser("force-upgrade", help="append a force-upgrade request")
    force.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    force.add_argument("--process", required=True)
    force.add_argument("--reason", default="external_trigger")
    force.add_argument("--target-version", default="auto")

    redis_ping = sub.add_parser("redis-ping", help="ping configured redis")
    redis_ping.add_argument("--config", default=DEFAULT_CONFIG_PATH)

    redis_get = sub.add_parser("redis-get", help="get redis string value")
    redis_get.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    redis_get.add_argument("--key", required=True)

    redis_set = sub.add_parser("redis-set", help="set redis string value")
    redis_set.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    redis_set.add_argument("--key", required=True)
    redis_set.add_argument("--value", required=True)
    redis_set.add_argument("--ttl-seconds", type=int, default=None)

    redis_del = sub.add_parser("redis-del", help="delete redis key")
    redis_del.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    redis_del.add_argument("--key", required=True)

    skill_request = sub.add_parser("request-skill-sync", help="append a skill sync request")
    skill_request.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    skill_request.add_argument("--skill", action="append", default=[])
    skill_request.add_argument("--reason", default="manual")

    report_request = sub.add_parser("request-report", help="append a runtime report request")
    report_request.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    report_request.add_argument("--type", default=REPORT_SANDBOX_STATUS)
    report_request.add_argument("--reason", default="manual")

    skill_runner = sub.add_parser("skill-sync-runner", help="run async skill sync task")
    skill_runner.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    skill_runner.add_argument("--request-id", required=True)
    skill_runner.add_argument("--reason", required=True)
    skill_runner.add_argument("--skill", action="append", default=[])
    return parser


def _load_config(path: str) -> dict:
    cfg = apply_defaults(read_config(path), APP_VERSION)
    errors = validate_config(cfg)
    if errors:
        raise ValueError("配置校验失败:\n- " + "\n- ".join(errors))
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(APP_VERSION)
        return 0
    if not args.command:
        print("缺少命令，使用 --help 查看", file=sys.stderr)
        return 2

    if args.command == "validate":
        try:
            _ = _load_config(args.config)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print("配置校验通过")
        return 0
    try:
        if args.command == "bootstrap":
            cfg = _load_config(args.config)
            return run_bootstrap(cfg, args.config)
        if args.command == "bootstrap-entry":
            cfg = _load_config(args.config)
            return run_bootstrap_entry(
                cfg,
                args.config,
                sandbox_started_at_ms=args.sandbox_started_at_ms,
                user_id=args.user_id,
                user_name=args.user_name,
                base_url=args.base_url,
                auth_token=args.auth_token,
                target_version=args.target_version,
                from_launcher=args.from_launcher,
            )
        if args.command == "launcher":
            return run_launcher(args.config)
        if args.command == "update-env":
            cfg = _load_config(args.config)
            return run_update_env(
                cfg,
                {
                    "sandbox_id": args.sandbox_id,
                    "sandbox_type": args.sandbox_type,
                    "sandbox_platform": args.sandbox_platform,
                    "expert_enable_ha": args.expert_enable_ha,
                    "user_id": args.user_id,
                    "user_name": args.user_name,
                    "base_url": args.base_url,
                    "auth_token": args.auth_token,
                    "startup_type": args.startup_type,
                    "sandbox_startup_duration_ms": args.sandbox_startup_duration_ms,
                    "sandbox_init_duration_ms": args.sandbox_init_duration_ms,
                },
                cfg_path=args.config,
            )
        if args.command == "daemon":
            return run_daemon_loop(
                args.config,
                APP_VERSION,
                once=args.once,
                interval=args.interval,
            )
        if args.command == "bootstrap-script-runner":
            return run_bootstrap_script_runner(args.name, args.script_command, args.timeout)
        if args.command == "upgrade-runner":
            cfg = _load_config(args.config)
            return run_upgrade_runner(cfg, args.process, args.target_version)
        if args.command == "version":
            print(APP_VERSION)
            return 0
        if args.command == "readiness":
            cfg = _load_config(args.config)
            result = check_process_readiness(cfg, args.process)
            print("true" if result.ready else "false")
            if not result.ready:
                print(result.message, file=sys.stderr)
            return 0 if result.ready else 1
        if args.command == "xagent-status":
            cfg = _load_config(args.config)
            result = get_xagent_status(cfg, max_age_seconds=args.max_age_seconds)
            print(json.dumps(result.to_payload(), ensure_ascii=False, separators=(",", ":")))
            return 0 if result.status == XAGENT_STATUS_READY else 1
        if args.command == "self-update":
            cfg = _load_config(args.config)
            return run_self_update(cfg, args.target_version, current_version=APP_VERSION)
        if args.command == "force-upgrade":
            cfg = _load_config(args.config)
            process = next(
                (
                    proc
                    for proc in cfg.get("processes", []) or []
                    if isinstance(proc, dict) and str(proc.get("name", "")).strip() == args.process
                ),
                None,
            )
            if process is not None:
                applicability = process_applicability(process, cfg)
                if not applicability.applicable:
                    print(
                        f"process is not applicable for this sandbox: {args.process}",
                        file=sys.stderr,
                    )
                    return 2
            path = str((cfg.get("runtime", {}) or {}).get("upgrade_request_file"))
            append_upgrade_request(path, process=args.process, reason=args.reason, target_version=args.target_version)
            print(
                f"force-upgrade request appended: process={args.process}, "
                f"target_version={args.target_version}, path={path}"
            )
            return 0
        if args.command == "redis-ping":
            cfg = _load_config(args.config)
            ok = build_redis_client(cfg).ping()
            print("PONG" if ok else "PING failed")
            return 0 if ok else 1
        if args.command == "redis-get":
            cfg = _load_config(args.config)
            value = build_redis_client(cfg).get(args.key)
            if value is None:
                return 1
            print(value)
            return 0
        if args.command == "redis-set":
            cfg = _load_config(args.config)
            ok = build_redis_client(cfg).set(args.key, args.value, ttl_seconds=args.ttl_seconds)
            print("OK" if ok else "SET failed")
            return 0 if ok else 1
        if args.command == "redis-del":
            cfg = _load_config(args.config)
            deleted = build_redis_client(cfg).delete(args.key)
            print(deleted)
            return 0
        if args.command == "request-skill-sync":
            cfg = _load_config(args.config)
            if not bool((cfg.get("skills", {}) or {}).get("enabled", False)):
                print("skill sync is disabled", file=sys.stderr)
                return 2
            if not skill_sync_applicable(cfg):
                print(f"skill sync is disabled for sandbox type: {sandbox_type_for_skills(cfg) or '-'}", file=sys.stderr)
                return 2
            path = str((cfg.get("runtime", {}) or {}).get("skill_request_file"))
            request_id = append_skill_request(path, list(args.skill or []), reason=args.reason)
            print(f"skill sync request appended: request_id={request_id}, path={path}")
            return 0
        if args.command == "request-report":
            cfg = _load_config(args.config)
            if not bool((cfg.get("report", {}) or {}).get("enabled", False)):
                print("report is disabled", file=sys.stderr)
                return 2
            report_type = append_report_request(cfg, args.type, reason=args.reason) or REPORT_SANDBOX_STATUS
            print(f"report request appended: type={report_type}")
            return 0
        if args.command == "skill-sync-runner":
            cfg = _load_config(args.config)
            if not bool((cfg.get("skills", {}) or {}).get("enabled", False)):
                print("skill sync is disabled", file=sys.stderr)
                return 2
            if not skill_sync_applicable(cfg):
                print(f"skill sync is disabled for sandbox type: {sandbox_type_for_skills(cfg) or '-'}", file=sys.stderr)
                return 2
            return run_skill_sync_runner(cfg, list(args.skill or []), args.request_id, args.reason)
    except Exception as exc:
        print(f"执行失败: {exc}", file=sys.stderr)
        return 1
    return 2
