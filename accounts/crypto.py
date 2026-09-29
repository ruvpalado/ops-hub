"""AES-256-GCM for tokens at rest. The only module in the codebase allowed to
import cryptography — CI greps for that invariant.

Layout of an encrypted blob: 12-byte nonce || ciphertext || 16-byte tag.
The token key is 32 random bytes, base64-encoded, supplied as OPSHUB_TOKEN_KEY
from real environment or from .env (dev). It is never generated here, never
defaulted, never logged; a missing or malformed key is a hard failure, because
starting with a *different* key would silently corrupt every stored token.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401  (choke point)

NONCE_LENGTH = 12
TAG_LENGTH = 16


class TokenKeyError(Exception):
    """OPSHUB_TOKEN_KEY is unusable: missing, malformed, or the wrong key."""


def token_key() -> bytes:
    raw = os.environ.get("OPSHUB_TOKEN_KEY", "").strip()
    if not raw:
        raise TokenKeyError("OPSHUB_TOKEN_KEY is not set; refusing to guess a key")
    try:
        key = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as exc:
        raise TokenKeyError("OPSHUB_TOKEN_KEY is not valid base64") from exc
    if len(key) != 32:
        raise TokenKeyError(f"OPSHUB_TOKEN_KEY must decode to 32 bytes, got {len(key)}")
    return key


def encrypt(plaintext: bytes) -> bytes:
    """Return nonce||ciphertext||tag. Caller stores the blob verbatim."""
    nonce = os.urandom(NONCE_LENGTH)
    return nonce + AESGCM(token_key()).encrypt(nonce, plaintext, None)


def decrypt(blob: bytes) -> bytes:
    """Inverse of encrypt. Tampering, truncation, and key mismatch all raise
    TokenKeyError — never a partial plaintext, so a corrupted token cannot
    silently misbehave."""
    if len(blob) < NONCE_LENGTH + TAG_LENGTH:
        raise TokenKeyError("encrypted blob too short to be valid")
    nonce, payload = blob[:NONCE_LENGTH], blob[NONCE_LENGTH:]
    try:
        return AESGCM(token_key()).decrypt(nonce, payload, None)
    except InvalidTag as exc:
        raise TokenKeyError("token decrypt failed: wrong key or tampered blob") from exc