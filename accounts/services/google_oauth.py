"""The two HTTPS calls the callback makes, and nothing else.

The token endpoint URL is a module constant, never a caller-supplied argument:
the authorization code, the client secret and the PKCE verifier all travel in
this request body, and a redirectable endpoint would be a straightforward
credential exfiltration. Tests patch the single HTTP primitive below.

Stdlib urllib rather than requests/httpx: two POSTs and a GET do not justify a
dependency, and the failure modes we care about (timeout, non-2xx, unparseable
body) are all handled explicitly here.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from accounts.oauth import TOKEN_ENDPOINT, USERINFO_ENDPOINT

HTTP_TIMEOUT_SECONDS = 15


class OAuthError(Exception):
    """Any failure in the token or userinfo leg. Never carries token material."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str
    email_verified: bool
    name: str = ""


def _http(method: str, url: str, *, data: dict | None = None, access_token: str | None = None) -> dict:
    """The one place that touches the network."""
    body = urllib.parse.urlencode(data).encode("ascii") if data else None
    request = urllib.request.Request(url, data=body, method=method)
    if data:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    if access_token:
        request.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # Google puts the machine-readable reason in the body's error field
        detail = ""
        try:
            detail = json.loads(exc.read()).get("error", "")
        except Exception:  # noqa: BLE001 - a broken error body must not mask the HTTP status
            detail = ""
        raise OAuthError(f"google returned HTTP {exc.code}", code=detail or None) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OAuthError("google unreachable", code="network") from exc

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise OAuthError("google returned a body we could not parse") from exc
    if not isinstance(payload, dict):
        raise OAuthError("google returned a body of the wrong shape")
    return payload


def exchange_code(
    *,
    code: str,
    code_verifier: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict:
    """Trade the authorization code for tokens. Returns Google's token payload."""
    payload = _http(
        "POST",
        TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
    )
    if not payload.get("access_token"):
        raise OAuthError("token response carried no access_token", code=payload.get("error"))
    return payload


def fetch_identity(access_token: str) -> Identity:
    payload = _http("GET", USERINFO_ENDPOINT, access_token=access_token)
    sub = str(payload.get("sub") or "")
    email = str(payload.get("email") or "")
    if not sub or not email:
        raise OAuthError("userinfo carried no sub/email")
    return Identity(
        sub=sub,
        email=email.lower(),
        email_verified=bool(payload.get("email_verified")),
        name=str(payload.get("name") or ""),
    )