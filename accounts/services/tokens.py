"""Token storage: what the callback received, encrypted and kept honest.

Two rules that exist because Google does not behave the way a naive
implementation assumes:

1. Google omits refresh_token from a token response when the grant it already
   has is still valid. Overwriting the stored token with nothing would log the
   user out on the next refresh, and resetting refresh_token_obtained_at would
   make the 7-day expiry warning permanently wrong. So a payload with no
   refresh_token leaves both columns exactly as they were.

2. Nothing here ever logs a token. The audit entry records which kinds landed,
   never the values, and the writer scrubs the same key names anyway.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from accounts import crypto
from core.models import Source

logger = logging.getLogger(__name__)

DEFAULT_EXPIRES_IN = 3600


def _expires_at(now, expires_in) -> object:
    return now + timedelta(seconds=int(expires_in or DEFAULT_EXPIRES_IN))


def store_tokens(user, tokens: dict, *, now=None) -> dict:
    """Persist a Google token payload onto the user row.

    Returns what actually changed, so the caller can audit it without reading
    the token values: {"access_token": True, "refresh_token": bool, "expires_at": ...}
    """
    now = now or timezone.now()
    updates = ["updated_at"]
    changed = {"access_token": False, "refresh_token": False, "token_expires_at": None}

    access_token = tokens.get("access_token")
    if access_token:
        user.access_token = crypto.encrypt(str(access_token).encode("utf-8"))
        user.token_expires_at = _expires_at(now, tokens.get("expires_in"))
        updates += ["access_token", "token_expires_at"]
        changed["access_token"] = True
        changed["token_expires_at"] = user.token_expires_at

    refresh_token = tokens.get("refresh_token")
    if refresh_token:
        # a real new refresh token: this is the only event that restarts the
        # 7-day clock, so only this branch writes the obtained_at stamp
        user.refresh_token = crypto.encrypt(str(refresh_token).encode("utf-8"))
        user.refresh_token_obtained_at = now
        updates += ["refresh_token", "refresh_token_obtained_at"]
        changed["refresh_token"] = True
    elif user.refresh_token:
        logger.info(
            "token response carried no refresh_token; keeping the stored one",
            extra={"app_user_id": user.pk},
        )

    user.save(update_fields=sorted(set(updates)))

    # a fresh grant re-activates the scheduler's rows and clears the error that
    # stopped them. DISCONNECTED is left alone: that is the user's own decision
    # to disconnect, and signing in again does not undo it.
    Source.objects.filter(
        user=user, status__in=[Source.Status.ACTIVE, Source.Status.ERROR]
    ).update(status=Source.Status.ACTIVE, last_error_code=None, next_attempt_at=now)
    Source.objects.filter(user=user, status=Source.Status.NEEDS_REAUTH).update(
        status=Source.Status.ACTIVE,
        last_error_code=None,
        disconnected_at=None,
        next_attempt_at=now,
    )
    return changed


def read_access_token(user) -> str | None:
    if not user.access_token:
        return None
    return crypto.decrypt(bytes(user.access_token)).decode("utf-8")


def read_refresh_token(user) -> str | None:
    if not user.refresh_token:
        return None
    return crypto.decrypt(bytes(user.refresh_token)).decode("utf-8")