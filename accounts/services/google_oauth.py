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


def _retry_after_seconds(raw: str | None) -> int | None:
    """`Retry-After` as a non-negative integer, or None.

    Only the delta-seconds form. The HTTP-date form is legal and Google does use
    it, but a date means "this long from now" relative to a clock this process
    does not share, and returning None makes the caller fall back to its own
    bounded backoff — which is the right answer for a value it cannot compute,
    and a wrong number here would be worse than no number.
    """
    if not raw:
        return None
    try:
        seconds = int(str(raw).strip())
    except ValueError:
        return None
    return max(seconds, 0)


class OAuthError(Exception):
    """Any failure in the token or userinfo leg. Never carries token material.

    `retry_after` is the `Retry-After` header, seconds as an int, or None. §6.2
    requires a 429 to defer the mailbox by exactly what Google asked for, and
    that number exists in exactly one place: the response header. A caller that
    cannot read it can only guess, and a guess is either too aggressive (retry
    inside the limit, and the limit gets longer) or silently caps the backoff at
    a local constant. So the header is parsed here, at the only place the
    response is still in hand, and travels on the exception.
    """

    def __init__(self, message: str, *, code: str | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str
    email_verified: bool
    name: str = ""


def _http(
    method: str,
    url: str,
    *,
    data: dict | None = None,
    json_body: dict | None = None,
    access_token: str | None = None,
) -> dict:
    """The one place that touches the network.

    `data` is form-encoded and `json_body` is JSON. Both exist because the two
    Google families disagree: the token endpoint only speaks form encoding, and
    the Gmail and Calendar REST APIs only speak JSON. A single code path with
    two encodings beats two code paths with one each, because the redaction and
    error-translation rules stay in one place.
    """
    if data is not None and json_body is not None:
        raise ValueError("pass data or json_body, not both")

    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        content_type = "application/json; charset=utf-8"
    else:
        body = urllib.parse.urlencode(data).encode("ascii") if data else None
        content_type = "application/x-www-form-urlencoded" if data else None

    request = urllib.request.Request(url, data=body, method=method)
    if content_type:
        request.add_header("Content-Type", content_type)
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
        # http_5xx stays distinguishable from invalid_grant: refresh retries one, not the other
        raise OAuthError(
            f"google returned HTTP {exc.code}",
            code=detail or f"http_{exc.code}",
            # read off the error response, the only one that carries it: a 2xx
            # with a Retry-After header has no meaning to any caller here
            retry_after=_retry_after_seconds(exc.headers.get("Retry-After")),
        ) from exc
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