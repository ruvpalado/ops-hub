"""Token key AES-256-GCM round trip, tamper detection, and key bootstrap."""

import base64
import os

import pytest
from django.core.management import call_command

from accounts.crypto import TokenKeyError, decrypt, encrypt, token_key

KEY = base64.b64encode(b"k" * 32).decode()


def test_roundtrip_returns_original(monkeypatch):
    monkeypatch.setenv("OPSHUB_TOKEN_KEY", KEY)
    blob = encrypt(b"ya29.refresh.token")
    assert decrypt(blob) == b"ya29.refresh.token"


def test_ciphertext_differs_and_nonce_unique(monkeypatch):
    monkeypatch.setenv("OPSHUB_TOKEN_KEY", KEY)
    blob1 = encrypt(b"same-plaintext")
    blob2 = encrypt(b"same-plaintext")
    assert blob1 != b"same-plaintext"
    assert blob1 != blob2  # fresh nonce each time


def test_tampered_blob_rejected(monkeypatch):
    monkeypatch.setenv("OPSHUB_TOKEN_KEY", KEY)
    blob = bytearray(encrypt(b"precious"))
    blob[20] ^= 0xFF  # flip one bit inside the ciphertext
    with pytest.raises(TokenKeyError):
        decrypt(bytes(blob))


def test_truncated_blob_rejected(monkeypatch):
    monkeypatch.setenv("OPSHUB_TOKEN_KEY", KEY)
    blob = encrypt(b"precious")
    with pytest.raises(TokenKeyError):
        decrypt(blob[:20])


def test_wrong_key_rejected(monkeypatch):
    monkeypatch.setenv("OPSHUB_TOKEN_KEY", KEY)
    blob = encrypt(b"precious")
    monkeypatch.setenv("OPSHUB_TOKEN_KEY", base64.b64encode(b"j" * 32).decode())
    with pytest.raises(TokenKeyError):
        decrypt(blob)


def test_missing_key_is_loud(monkeypatch):
    monkeypatch.delenv("OPSHUB_TOKEN_KEY", raising=False)
    with pytest.raises(TokenKeyError):
        token_key()


def test_set_token_key_generates_usable_key(tmp_path, monkeypatch):
    import io

    env_file = tmp_path / ".env"
    env_file.write_text("OPSHUB_ENV=development\n")
    out = io.StringIO()
    call_command("set_token_key", "--write-env", env_file=str(env_file), stdout=out)
    assert "restart the process" in out.getvalue()
    line = [l for l in env_file.read_text().splitlines() if l.startswith("OPSHUB_TOKEN_KEY=")][0]
    raw_key = line.split("=", 1)[1]
    monkeypatch.setenv("OPSHUB_TOKEN_KEY", raw_key)
    assert decrypt(encrypt(b"roundtrip")) == b"roundtrip"