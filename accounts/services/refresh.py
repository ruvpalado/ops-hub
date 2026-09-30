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
from accounts.models import AppUser
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

# Testing-mode refresh tokens live 7 days. Warn in the last N of them.
TOKEN_MAX_AGE_DAYS = 7
TOKEN_WARN_BEFORE_DAYS = 2


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


# ------------------------------------------------ step 13: Testing mode is normal
#
# A Google OAuth app in "Testing" publishing status hands out refresh tokens
# that expire in 7 days. That is not an edge case for this build; it is the
# operating condition, and the product has to name it rather than reporting a
# generic failure. Two helpers, both read-only, because the decision is a
# product decision:

#   classify_grant_loss  why a grant went away: expiry past 7 days, or a
#                        revocation while the token was still young. The user
#                        can act on one and not the other.
#   sweep_expiring_tokens  who is about to be hit, so the banner can be shown
#                        before the failure rather than after it.

def token_age(user, *, now=None) -> timedelta | None:
    if not user.refresh_token_obtained_at:
        return None
    return (now or timezone.now()) - user.refresh_token_obtained_at


def classify_grant_loss(user, *, now=None) -> str:
    """"expiry" once the stored refresh token is past its 7-day life, else "revocation"."""
    age = token_age(user, now=now)
    if age is None:
        return "revocation"
    return "expiry" if age >= timedelta(days=TOKEN_MAX_AGE_DAYS) else "revocation"


def _warning_cutoff(now):
    """Oldest obtained_at still inside the warning window."""
    return now - timedelta(days=max(TOKEN_MAX_AGE_DAYS - TOKEN_WARN_BEFORE_DAYS, 0))


# The copy the user reads. Two messages, not one, and each names its own cause:
# a Testing-mode expiry is the app's own 7-day rule and the fix is a button,
# while a revocation means somebody or something withdrew consent and no button
# in this app will bring it back. Blurring them into "sync error" is the one
# thing that must not happen.
BANNER_EXPIRY_MESSAGE = (
    "Your Google connection expires every 7 days while this app is in Google's "
    "testing mode. It needs reconnecting now."
)
BANNER_REVOCATION_MESSAGE = (
    "Access to your Google account was revoked, so the app can no longer read it. "
    "Reconnect to grant access again."
)
BANNER_EXPIRING_MESSAGE = (
    "Your Google connection expires in under 2 days. Reconnect early to keep your "
    "calendar and inbox in sync."
)

STOPPED_STATUSES = (Source.Status.NEEDS_REAUTH, Source.Status.ERROR)


def banner_for(user, *, now=None) -> dict | None:
    """What this user needs to be told, or None when they need nothing.

    Banner first, failure second: a user still inside the warning window gets
    the proactive message, and a stopped user gets the cause they can act on.
    Returns {"kind", "message", "show_action"} for templates/partials/
    reconnect_banner.html, which is the only thing that renders it.
    """
    now = now or timezone.now()
    statuses = set(
        Source.objects.filter(user=user)
        .exclude(status=Source.Status.DISCONNECTED)
        .values_list("status", flat=True)
    )
    if not statuses:
        return None

    if statuses & set(STOPPED_STATUSES):
        cause = classify_grant_loss(user, now=now)
        return {
            "kind": cause,
            "message": BANNER_EXPIRY_MESSAGE if cause == "expiry" else BANNER_REVOCATION_MESSAGE,
            "show_action": True,
        }

    age = token_age(user, now=now)
    if age is not None and user.refresh_token_obtained_at <= _warning_cutoff(now):
        return {"kind": "expiring", "message": BANNER_EXPIRING_MESSAGE, "show_action": False}
    return None


def sweep_expiring_tokens(*, now=None) -> list[tuple[AppUser, str]]:
    """Users whose active refresh token is inside the warning window.

    Returns (user, reason) pairs, newest risk last. Advisory only: this touches
    nothing, it just produces the work list the scheduler or a notifier reads.
    """
    now = now or timezone.now()
    cutoff = _warning_cutoff(now)
    rows = (
        Source.objects.filter(status=Source.Status.ACTIVE, user__refresh_token_obtained_at__lte=cutoff)
        .select_related("user")
        .order_by("user__refresh_token_obtained_at")
    )
    seen: dict[int, tuple[AppUser, str]] = {}
    for source in rows:
        seen.setdefault(source.user_id, (source.user, classify_grant_loss(source.user, now=now)))
    return list(seen.values())
