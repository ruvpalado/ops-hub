"""Google OAuth handshake, authorization-code + PKCE.

Everything here is parameter construction and verification. The token endpoint
itself is a plain HTTPS POST to Google in services.google_oauth — kept out of
this module so that nothing in the URL-building path can ever be pointed at a
different host by a careless edit.

Scope discipline: exactly three scopes, and the ones Google offers as
read-only wherever a read-only variant exists. Nothing in this file may add a
write scope to calendar or gmail.calendar; gmail.send is the single deliberate
write, needed to file a task as a draft email.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from urllib.parse import urlencode

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

# The only three scopes this application asks for, in a fixed order so the
# authorization URL is stable and testable.
SCOPES = ("gmail.readonly", "gmail.send", "calendar.readonly")

STATE_BYTES = 32
VERIFIER_BYTES = 64  # 86 base64url chars, inside RFC 7636's 43..128 window
STATE_SESSION_KEY = "oauth_state"
VERIFIER_SESSION_KEY = "oauth_code_verifier"
STATE_MAX_AGE_SECONDS = 600


def new_state() -> str:
    """A 32-byte CSPRNG nonce. The whole CSRF story rests on this value."""
    return secrets.token_urlsafe(STATE_BYTES)


def new_code_verifier() -> str:
    return secrets.token_urlsafe(VERIFIER_BYTES)


def code_challenge(verifier: str) -> str:
    """S256 challenge: base64url(sha256(verifier)), no padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def state_matches(given: str | None, expected: str | None) -> bool:
    """Constant-time comparison. A timing oracle on a 32-byte state is small,
    but it is free to close, and 'small but free' is the whole standard here."""
    if not given or not expected:
        return False
    return hmac.compare_digest(given, expected)


def authorization_url(*, client_id: str, redirect_uri: str, state: str, code_verifier: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # offline + consent: without both, Google withholds a refresh token,
        # which is precisely the token Testing-mode users lose after 7 days.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
        "state": state,
        "code_challenge": code_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}"