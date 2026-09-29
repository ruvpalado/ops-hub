"""Refreshing Google tokens, and what each failure costs.

Four outcomes, and the whole design is the difference between them:

  skipped      the stored access token is still comfortably valid. No call is
               made: a no-op refresh is a wasted round-trip and a wasted quota
               unit, and a token that is not near expiry is not a problem.
  refreshed    a real token swap. The access token and its expiry move; the
               refresh token is left alone, because Google's refresh tokens
               live for months and reissuing one is not our business.
  needs_reauth Google said invalid_grant. The user must consent again. This is
               terminal for automatic work: no retry, no backoff, and no user
               data is touched. A revoked grant is a decision, not a fault.
  deferred     the network or Google had a bad moment. Backoff 15 minutes,
               stay connected. Flaky infrastructure must never cost a user
               their sync.

Retry timing for the transient case is 2s then 8s, and the sleep is injected so
the test suite does not spend ten seconds proving the arithmetic.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from accounts.services import google_oauth
from accounts.services.google_oauth import OAuthError
from accounts.services.tokens import read_refresh_token, store_tokens
from core.models import Source

logger = logging.getLogger(__name__)

OUTCOME_SKIPPED = "skipped"
OUTCOME_REFRESHED = "refreshed"
OUTCOME_NEEDS_REAUTH = "needs_reauth"
OUTCOME_DEFERRED = "deferred"

# how close to expiry counts as "refresh now"; under a minute the request would
# be racing the clock it is trying to beat
REFRESH_MARGIN_SECONDS = 60
RETRY_DELAYS_SECONDS = (2, 8)
DEFER_MINUTES = 15


def _is_transient(exc: OAuthError) -> bool:
    code = exc.code or ""
    return code == "network" or code.startswith("http_5")


def _post_token(data: dict, *, sleep) -> dict:
    """POST to the token endpoint, retrying transient failures twice."""
    attempts = (None, *RETRY_DELAYS_SECONDS)
    last: OAuthError | None = None
    for delay in attempts:
        if delay:
            sleep(delay)
        try:
            return google_oauth._http("POST", google_oauth.TOKEN_ENDPOINT, data=data)
        except OAuthError as exc:
            if not _is_transient(exc):
                raise
            last = exc
    raise last  # pragma: no cover - loop always returns or raises


def _needs_reauth(user, code: str, now) -> None:
    """Terminal state. Next attempt is None, so the scheduler never picks it up."""
    Source.objects.filter(user=user).exclude(status=Source.Status.DISCONNECTED).update(
        status=Source.Status.NEEDS_REAUTH,
        last_error_code=code,
        disconnected_at=now,
        next_attempt_at=None,
    )


def refresh_if_needed(user, *, now=None, sleep=time.sleep) -> str:
    """Ensure the user has a usable access token. Returns one of the OUTCOME_* values."""
    now = now or timezone.now()

    if not user.refresh_token:
        _needs_reauth(user, "no_token", now)
        return OUTCOME_NEEDS_REAUTH

    if user.token_expires_at and user.token_expires_at - now > timedelta(seconds=REFRESH_MARGIN_SECONDS):
        return OUTCOME_SKIPPED

    try:
        payload = _post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": read_refresh_token(user) or "",
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
            },
            sleep=sleep,
        )
    except OAuthError as exc:
        if _is_transient(exc):
            code = exc.code or "network"
            Source.objects.filter(user=user).exclude(status=Source.Status.DISCONNECTED).update(
                next_attempt_at=now + timedelta(minutes=DEFER_MINUTES),
                # the audit vocabulary is fixed, so http_5xx lands as upstream_5xx
                last_error_code="network" if code == "network" else "upstream_5xx",
            )
            logger.warning("refresh deferred", extra={"app_user_id": user.pk, "refresh_error": code})
            return OUTCOME_DEFERRED
        # invalid_grant and friends: the grant is gone, and it stays gone
        code = exc.code or "invalid_grant"
        _needs_reauth(user, code if code in ("invalid_grant", "no_token") else "invalid_grant", now)
        logger.warning("grant unusable; reauth required", extra={"app_user_id": user.pk, "refresh_error": code})
        return OUTCOME_NEEDS_REAUTH

    store_tokens(user, payload, now=now)
    return OUTCOME_REFRESHED
