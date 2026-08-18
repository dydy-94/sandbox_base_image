from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .environment import SandboxContext
from .verifier import TokenVerificationError, TrustedTokenVerifier


MAX_TOKEN_BYTES = 32 * 1024


@dataclass(frozen=True)
class AuthorizationResult:
    status: int
    reason: str


ALLOW = AuthorizationResult(204, "allow")


def authorize(
    context: SandboxContext | None,
    token: str | None,
    verifier: TrustedTokenVerifier,
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
        payload = verifier.decode(token)
    except TokenVerificationError as exc:
        return AuthorizationResult(401, exc.reason)

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
        return AuthorizationResult(403, "missing_user_claim")
    if _normalize_id(token_user_id) == _normalize_id(context.user_id):
        return ALLOW
    return AuthorizationResult(403, "user_mismatch")


def _claim_text(value: object) -> str:
    if value is None or isinstance(value, (bool, dict, list)):
        return ""
    return str(value).strip()


def _normalize_id(value: str) -> str:
    return value.strip().lower()
