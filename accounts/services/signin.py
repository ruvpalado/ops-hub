"""Sign-in identity resolution: Google says who you are, this decides whether
that identity gets an account. Views call this and nothing else.

Step 9 form: an existing account is signed in by email; an unknown verified
email gets a bare account. The invite gate and Source provisioning land on top
of this function in step 10 — the contract (verified Identity in, AppUser out,
or None when refused) is stable across that change.
"""

from __future__ import annotations

import logging

from accounts.models import AppUser
from accounts.services.google_oauth import Identity

logger = logging.getLogger(__name__)


def resolve_identity(identity: Identity) -> AppUser | None:
    """Return the AppUser this identity may sign in as, or None if refused.

    Keys on google_sub first: a user who changes their Workspace email keeps
    their account and their sources.
    """
    if not identity.email_verified:
        return None

    user = AppUser.objects.filter(google_sub=identity.sub).first()
    if user is None:
        user = AppUser.objects.filter(email=identity.email).first()
    if user is None:
        user = AppUser.objects.create(
            email=identity.email,
            display_name=identity.name or identity.email,
            google_sub=identity.sub,
        )
        logger.info("provisioned AppUser", extra={"app_user_id": user.pk})
        return user

    # first sign-in after the sub appears (or after a Workspace rename): bind it
    if not user.google_sub:
        user.google_sub = identity.sub
        user.save(update_fields=["google_sub", "updated_at"])
    if identity.name and user.display_name != identity.name:
        user.display_name = identity.name
        user.save(update_fields=["display_name", "updated_at"])
    return user