"""Symmetric encryption for secrets at rest (NFR-S-03, FR-CW-12).

AES-256-GCM with a key from the environment (`PAYINTEL_SECRETS__ENCRYPTION_KEY`,
base64-encoded 32 bytes). Ciphertext layout: `v1:` + base64(nonce || ciphertext||tag).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from payintel.core.errors import ConfigurationError

_PREFIX = "v1:"
_NONCE_BYTES = 12


class SecretBox:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ConfigurationError("encryption key must be exactly 32 bytes")
        self._aead = AESGCM(key)

    @classmethod
    def from_base64(cls, key_b64: str) -> SecretBox:
        if not key_b64:
            raise ConfigurationError("PAYINTEL_SECRETS__ENCRYPTION_KEY is not set")
        try:
            raw = base64.b64decode(key_b64, validate=True)
        except ValueError as exc:
            raise ConfigurationError("encryption key is not valid base64") from exc
        return cls(raw)

    def encrypt(self, plaintext: str, *, associated_data: str = "") -> str:
        nonce = os.urandom(_NONCE_BYTES)
        ct = self._aead.encrypt(nonce, plaintext.encode(), associated_data.encode() or None)
        return _PREFIX + base64.b64encode(nonce + ct).decode()

    def decrypt(self, token: str, *, associated_data: str = "") -> str:
        if not token.startswith(_PREFIX):
            raise ValueError("unknown ciphertext version")
        blob = base64.b64decode(token[len(_PREFIX) :])
        nonce, ct = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        return self._aead.decrypt(nonce, ct, associated_data.encode() or None).decode()


def hash_api_key(raw_key: str, pepper: str) -> str:
    """SHA-256 of the API key with a secret pepper (NFR-S-02). Returned as hex."""
    if not pepper:
        raise ConfigurationError("PAYINTEL_SECRETS__API_KEY_PEPPER is not set")
    return hmac.new(pepper.encode(), raw_key.encode(), hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
