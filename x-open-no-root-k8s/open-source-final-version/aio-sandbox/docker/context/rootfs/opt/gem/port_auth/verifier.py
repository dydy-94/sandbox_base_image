from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jwt
from jwt import InvalidSignatureError, InvalidTokenError, MissingRequiredClaimError, PyJWK


TRUSTED_JWKS_PATH = Path(__file__).with_name("trusted_jwks.json")
ALGORITHM = "RS256"


class TokenVerificationError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TrustedTokenVerifier:
    def __init__(self, keys: dict[str, PyJWK]) -> None:
        if not keys:
            raise ValueError("trusted JWKS contains no usable keys")
        self._keys = keys

    @classmethod
    def from_path(cls, path: Path = TRUSTED_JWKS_PATH) -> TrustedTokenVerifier:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_jwks(payload)

    @classmethod
    def from_jwks(cls, payload: object) -> TrustedTokenVerifier:
        if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
            raise ValueError("trusted JWKS must contain a keys array")

        keys: dict[str, PyJWK] = {}
        for raw_key in payload["keys"]:
            if not isinstance(raw_key, dict):
                raise ValueError("trusted JWK must be an object")
            kid = raw_key.get("kid")
            if not isinstance(kid, str) or not kid:
                raise ValueError("trusted JWK must contain a non-empty kid")
            if kid in keys:
                raise ValueError(f"duplicate trusted JWK kid: {kid}")
            if raw_key.get("kty") != "RSA" or raw_key.get("alg") != ALGORITHM:
                raise ValueError(f"unsupported trusted JWK: {kid}")
            keys[kid] = PyJWK.from_dict(raw_key, algorithm=ALGORITHM)
        return cls(keys)

    def decode(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except InvalidTokenError as exc:
            raise TokenVerificationError("malformed_token") from exc

        if header.get("alg") != ALGORITHM:
            raise TokenVerificationError("unsupported_algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenVerificationError("missing_kid")
        signing_key = self._keys.get(kid)
        if signing_key is None:
            raise TokenVerificationError("unknown_kid")

        try:
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=[ALGORITHM],
                options={
                    "require": ["exp"],
                    "verify_exp": False,
                    "verify_aud": False,
                },
            )
        except MissingRequiredClaimError as exc:
            raise TokenVerificationError("invalid_exp") from exc
        except InvalidSignatureError as exc:
            raise TokenVerificationError("invalid_signature") from exc
        except InvalidTokenError as exc:
            raise TokenVerificationError("malformed_token") from exc
        if not isinstance(payload, dict):
            raise TokenVerificationError("malformed_token")
        return payload
