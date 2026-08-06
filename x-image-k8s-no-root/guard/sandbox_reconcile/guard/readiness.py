from __future__ import annotations

"""External readiness probe helpers."""

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .process_applicability import process_applicability


@dataclass
class ReadinessResult:
    ready: bool
    message: str


def _find_process(cfg: dict[str, Any], name: str) -> dict[str, Any] | None:
    for proc in cfg.get("processes", []) or []:
        if isinstance(proc, dict) and str(proc.get("name", "")).strip() == name:
            return proc
    return None


def _http_json_health_check(health: dict[str, Any]) -> ReadinessResult:
    method = str(health.get("method", "GET")).strip().upper() or "GET"
    if method not in {"GET", "POST"}:
        return ReadinessResult(False, f"unsupported health probe method: {method}")
    url = str(health.get("url", "")).strip()
    expect_field = str(health.get("expect_json_field", "")).strip()
    expect_value = health.get("expect_json_value", "")
    expect_array_contains = health.get("expect_json_array_contains")
    timeout_seconds = max(1, int(health.get("timeout_seconds", 3)))
    if not url or (not expect_field and not isinstance(expect_array_contains, dict)):
        return ReadinessResult(False, "health probe config is incomplete")

    headers = {"Accept": "application/json"}
    configured_headers = health.get("headers")
    if isinstance(configured_headers, dict):
        for key, value in configured_headers.items():
            header_key = str(key).strip()
            if header_key:
                headers[header_key] = str(value)

    body: bytes | None = None
    if "body_json" in health:
        body = json.dumps(health.get("body_json"), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif "body" in health:
        body = str(health.get("body", "")).encode("utf-8")

    req = Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(req, timeout=float(timeout_seconds)) as resp:
            status = int(getattr(resp, "status", 200))
            if status < 200 or status >= 300:
                return ReadinessResult(False, f"http status {status}")
            raw = resp.read()
    except HTTPError as exc:
        return ReadinessResult(False, f"http status {exc.code}")
    except URLError as exc:
        return ReadinessResult(False, f"http request failed: {exc.reason}")
    except TimeoutError:
        return ReadinessResult(False, "http request timed out")
    except Exception as exc:
        return ReadinessResult(False, f"http request failed: {exc}")

    try:
        payload = json.loads(raw.decode("utf-8") if raw else "{}")
    except Exception:
        return ReadinessResult(False, "health probe response is not valid json")

    if isinstance(expect_array_contains, dict):
        if not isinstance(payload, list):
            return ReadinessResult(False, "health probe response json must be array")
        for item in payload:
            if not isinstance(item, dict):
                continue
            if all(item.get(str(key)) == expected or str(item.get(str(key))) == str(expected) for key, expected in expect_array_contains.items()):
                return ReadinessResult(True, "health probe ok")
        return ReadinessResult(False, f"health probe array does not contain {expect_array_contains!r}")

    if not isinstance(payload, dict):
        return ReadinessResult(False, "health probe response json must be object")
    actual = payload.get(expect_field)
    if actual != expect_value and str(actual) != str(expect_value):
        return ReadinessResult(False, f"health probe {expect_field}={actual!r}, expect={expect_value!r}")
    return ReadinessResult(True, "health probe ok")


def check_process_readiness(cfg: dict[str, Any], process_name: str = "xagent") -> ReadinessResult:
    proc = _find_process(cfg, process_name)
    if proc is None:
        return ReadinessResult(False, f"process not configured: {process_name}")
    applicability = process_applicability(proc, cfg)
    if not applicability.applicable:
        return ReadinessResult(False, f"process not applicable: {process_name}")
    health = proc.get("health_check")
    if not isinstance(health, dict):
        return ReadinessResult(False, f"process has no health_check: {process_name}")
    health_type = str(health.get("type", "")).strip()
    if health_type != "http_json":
        return ReadinessResult(False, f"unsupported health_check type: {health_type or '-'}")
    return _http_json_health_check(health)
