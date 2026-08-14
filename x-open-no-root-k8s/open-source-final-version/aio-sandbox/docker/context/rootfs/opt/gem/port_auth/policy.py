from __future__ import annotations

import base64
import json
import math
import time
from dataclasses import dataclass

from .environment import SandboxContext


MAX_TOKEN_BYTES = 32 * 1024


@dataclass(frozen=True)
class AuthorizationResult:
    status: int
    reason: str


ALLOW = AuthorizationResult(204, "allow")


def authorize(
    context: SandboxContext | None,
    token: str | None,
    *,
    now: float | None = None,
) -> AuthorizationResult:
    if context is None or context.sandbox_type != "USER":
        return ALLOW
    if not token:
        return AuthorizationResult(401, "missing_token")
    if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
        return AuthorizationResult(401, "token_too_large")

    try:
        header, payload = _decode_jwt(token)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return AuthorizationResult(401, "malformed_token")
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return AuthorizationResult(401, "malformed_token")

    expires_at = payload.get("exp")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return AuthorizationResult(401, "invalid_exp")
    if not math.isfinite(float(expires_at)):
        return AuthorizationResult(401, "invalid_exp")
    if (time.time() if now is None else now) >= float(expires_at):
        return AuthorizationResult(401, "expired_token")

    subject = _claim_text(payload.get("sub"))
    if subject and subject.lower().endswith("@native"):
        return ALLOW

    token_user_id = _claim_text(payload.get("sap_id"))
    if not token_user_id:
        token_user_id = _claim_text(payload.get("rtc_id"))
    if not token_user_id:
        return ALLOW
    if _normalize_id(token_user_id) == _normalize_id(context.user_id):
        return ALLOW
    return AuthorizationResult(403, "user_mismatch")


def _decode_jwt(token: str) -> tuple[object, object]:
    parts = token.strip().split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError("JWT must contain three non-empty segments")
    return _decode_segment(parts[0]), _decode_segment(parts[1])


def _decode_segment(segment: str) -> object:
    padding = "=" * (-len(segment) % 4)
    decoded = base64.b64decode(segment + padding, altchars=b"-_", validate=True)
    return json.loads(decoded.decode("utf-8"))


def _claim_text(value: object) -> str:
    if value is None or isinstance(value, (bool, dict, list)):
        return ""
    return str(value).strip()


def _normalize_id(value: str) -> str:
    return value.strip().lower()
