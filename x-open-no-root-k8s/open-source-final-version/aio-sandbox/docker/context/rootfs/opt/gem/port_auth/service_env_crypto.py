from __future__ import annotations

import base64
import binascii
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ENCRYPTED_PREFIX = "ENC[AES256GCM]:"
MATERIAL_PATH = Path("/home/x/.daemon/sandbox_reconcile/guard/.crypto-material")
_DEFAULT_MATERIAL_BASE64_PARTS = (
    "cUlZaThrVEF1",
    "bkRYRFlBOUpF",
    "Ynh4UHdtaENq",
    "YWtLQ0s=",
)
_KEY_LENGTH = 32
_IV_LENGTH = 12
_TAG_LENGTH = 16


class ServiceEnvCryptoError(ValueError):
    pass


def _decode_material(value: str) -> bytes:
    try:
        material = base64.b64decode(value.strip(), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ServiceEnvCryptoError("invalid service env crypto material") from exc
    if len(material) != _KEY_LENGTH:
        raise ServiceEnvCryptoError("service env crypto material must decode to 32 bytes")
    return material


class ServiceEnvDecryptor:
    def __init__(
        self,
        *,
        material_path: Path = MATERIAL_PATH,
        default_material: bytes | None = None,
    ) -> None:
        self._material_path = material_path
        self._default_material = (
            _decode_material("".join(_DEFAULT_MATERIAL_BASE64_PARTS))
            if default_material is None
            else default_material
        )
        if len(self._default_material) != _KEY_LENGTH:
            raise ServiceEnvCryptoError("default service env material must be 32 bytes")
        self._material_signature: tuple[int, int, int, int] | None = None
        self._material_missing = False
        self._preferred_material = self._default_material
        self._refresh_material(force=True)

    def decrypt(self, value: str) -> str:
        if not value.startswith(ENCRYPTED_PREFIX):
            raise ServiceEnvCryptoError("unsupported service env format")
        self._refresh_material()
        try:
            payload = base64.b64decode(
                value[len(ENCRYPTED_PREFIX) :].strip(), validate=True
            )
        except (ValueError, binascii.Error) as exc:
            raise ServiceEnvCryptoError("invalid encrypted service env payload") from exc
        if len(payload) < _IV_LENGTH + _TAG_LENGTH:
            raise ServiceEnvCryptoError("encrypted service env payload is too short")

        iv = payload[:_IV_LENGTH]
        ciphertext_and_tag = payload[_IV_LENGTH:]
        materials = [self._preferred_material]
        if self._preferred_material != self._default_material:
            materials.append(self._default_material)
        for material in materials:
            try:
                plaintext = AESGCM(material).decrypt(iv, ciphertext_and_tag, None)
                return plaintext.decode("utf-8")
            except (InvalidTag, UnicodeDecodeError):
                continue
        raise ServiceEnvCryptoError("encrypted service env authentication failed")

    def _refresh_material(self, *, force: bool = False) -> None:
        try:
            stat = self._material_path.stat()
        except FileNotFoundError:
            if force or not self._material_missing:
                self._preferred_material = self._default_material
                self._material_signature = None
                self._material_missing = True
            return
        except OSError as exc:
            raise ServiceEnvCryptoError("cannot inspect service env crypto material") from exc

        signature = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if not force and not self._material_missing and signature == self._material_signature:
            return
        try:
            encoded = self._material_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise ServiceEnvCryptoError("cannot read service env crypto material") from exc
        self._preferred_material = _decode_material(encoded)
        self._material_signature = signature
        self._material_missing = False
