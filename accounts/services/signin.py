"""Sign-in identity resolution: Google says who you are, this decides whether
that identity gets an account. Views call this and nothing else.

The rule is invite-or-claim, and it is deliberately boring:

* a placeholder account (an admin pre-created the email) is claimed the moment
  the matching verified Google identity shows up;
* a verified email in an allowed Workspace domain may claim a fresh account;
* everything else is refused.

The allowlist is an env var, not a table. An invitation table is a feature
nobody has asked for; if invites ever need per-user roles and expiry, that is
the step that adds it, and it does not change this signature.

Fails closed: an unset allowlist permits nobody, because an env var that
defaults to "open" turns a missing secret into a data breach.
"""

from __future__ import annotations

import logging

from core.models import Source

from accounts.models import AppUser
from accounts.services.google_oauth import Identity

logger = logging.getLogger(__name__)


def domain_allowed(email: str, domains: list[str]) -> bool:
    if not domains:
        return False
    domain = email.rsplit("@", 1)[-1].lower()
    return any(domain == d.lower() for d in domains)


def session_user(user_id) -> AppUser | None:
    """The signed-in AppUser, or None if there is no such row any more.

    The session stores a bare pk, so something has to turn it back into a row.
    That lookup belongs here rather than in a view, which holds no ORM queries.
    A None is a real answer: the account was deleted while the cookie lived.
    """
    if not user_id:
        return None
    return AppUser.objects.filter(pk=user_id).first()


def ensure_sources(user: AppUser) -> list[Source]:
    """Every account has both source slots from first sign-in onwards.

    Creating them up front is what lets the sync workers enumerate work by
    filtering on status, instead of guessing which users have connected
    anything.
    """
    return [Source.objects.get_or_create(user=user, kind=kind)[0] for kind in ("gmail", "calendar")]


def resolve_identity(identity: Identity) -> AppUser | None:
    """Return the AppUser this identity may sign in as, or None if refused.

    Keys on google_sub first: a user who changes their Workspace email keeps
    their account and their sources.
    """
    from django.conf import settings

    if not identity.email_verified:
        return None

    user = AppUser.objects.filter(google_sub=identity.sub).first()

    if user is None:
        # claiming a pre-created account, if one waits for this email
        user = AppUser.objects.filter(email=identity.email).first()
        if user is not None and user.google_sub not in (None, identity.sub):
            return None  # this address is already bound to a different identity

    if user is None:
        if not domain_allowed(identity.email, settings.ALLOWED_EMAIL_DOMAINS):
            logger.info(
                "refused sign-in: domain not allowed",
                extra={"signin_domain": identity.email.rsplit("@", 1)[-1]},
            )
            return None
        user = AppUser.objects.create(
            email=identity.email,
            display_name=identity.name or identity.email,
            google_sub=identity.sub,
        )
        logger.info("provisioned AppUser", extra={"app_user_id": user.pk})
    else:
        # first sign-in after the sub appears, or after a Workspace rename
        updates = []
        if not user.google_sub:
            user.google_sub = identity.sub
            updates.append("google_sub")
        if identity.name and user.display_name != identity.name:
            user.display_name = identity.name
            updates.append("display_name")
        if updates:
            updates.append("updated_at")
            user.save(update_fields=updates)

    ensure_sources(user)
    return user