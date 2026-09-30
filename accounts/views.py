"""Views for the Google sign-in handshake. No ORM queries live here: a view
builds a request, hands it to a service or to accounts.oauth, and returns a
response. The session is the only state a view touches.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponseBadRequest, HttpResponseForbidden, HttpResponseRedirect, HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_GET

from accounts import oauth
from accounts import crypto
from accounts.services import google_oauth, signin
from accounts.services import tokens as token_store
from audit import writer as audit
from core.permissions import SESSION_USER_KEY  # re-exported: this module writes it, the decorators read it

logger = logging.getLogger(__name__)

# Marks a callback as the second leg of /reconnect rather than a fresh sign-in.
# It lives in the session for the same reason the state and verifier do: it is
# part of one round trip and is consumed by the leg that closes it.
RECONNECT_SESSION_KEY = "reconnect_intent"


def _oauth_config() -> tuple[str, str]:
    from django.conf import settings

    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_REDIRECT_URI:
        raise ImproperlyConfigured(
            "GOOGLE_CLIENT_ID and GOOGLE_REDIRECT_URI must be set to run sign-in"
        )
    return settings.GOOGLE_CLIENT_ID, settings.GOOGLE_REDIRECT_URI


@require_GET
def google_start(request):
    """Mint a state + PKCE verifier, stash both in the session, redirect to Google."""
    client_id, redirect_uri = _oauth_config()
    state = oauth.new_state()
    verifier = oauth.new_code_verifier()
    request.session[oauth.STATE_SESSION_KEY] = state
    request.session[oauth.VERIFIER_SESSION_KEY] = verifier
    return HttpResponseRedirect(
        oauth.authorization_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_verifier=verifier,
        )
    )


@require_GET
def reconnect(request):
    """Start the consent round trip that replaces a dead Testing-mode grant.

    This is the "Reconnect Google" button's whole job, and it is the start leg
    only. Google matches redirect_uri exactly and the client has one registered
    address, so the trip comes back to the ordinary callback with the intent
    marked in the session. A dead 7-day grant is a sign-in-shaped problem:
    same state machine, same exchange, no second copy of it to keep correct.
    """
    if not request.session.get(SESSION_USER_KEY):
        return redirect("/auth/google", permanent=False)
    request.session[RECONNECT_SESSION_KEY] = True
    return google_start(request)


def _plain(message: str, status: int = 400) -> HttpResponse:
    return HttpResponse(message, status=status, content_type="text/plain; charset=utf-8")


@require_GET
def google_callback(request):
    """Verify the round trip, then decide whether the identity gets in.

    The state and verifier are popped before any validation: one attempt, one
    nonce. A failed attempt cannot be retried with the same cookie, and a
    replayed callback finds an empty session.
    """
    from django.conf import settings

    state = request.session.pop(oauth.STATE_SESSION_KEY, None)
    verifier = request.session.pop(oauth.VERIFIER_SESSION_KEY, None)
    reconnecting = request.session.pop(RECONNECT_SESSION_KEY, False)

    if request.GET.get("error"):
        # Google refused or the user declined; not an exception, a normal outcome
        audit.record(actor=None, action="auth.signin_denied", target_type="google", metadata={"reason": "provider_error"})
        return _plain("Sign-in was cancelled or denied by Google.", 400)

    if not oauth.state_matches(request.GET.get("state"), state):
        audit.record(actor=None, action="auth.signin_denied", target_type="google", metadata={"reason": "state_mismatch"})
        return _plain("Sign-in failed: state mismatch.", 400)

    if not verifier:
        audit.record(actor=None, action="auth.signin_denied", target_type="google", metadata={"reason": "missing_verifier"})
        return _plain("Sign-in failed: state mismatch.", 400)

    code = request.GET.get("code")
    if not code:
        audit.record(actor=None, action="auth.signin_denied", target_type="google", metadata={"reason": "missing_code"})
        return _plain("Sign-in failed: no authorization code.", 400)

    try:
        token_payload = google_oauth.exchange_code(
            code=code,
            code_verifier=verifier,
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            redirect_uri=settings.GOOGLE_REDIRECT_URI,
        )
        identity = google_oauth.fetch_identity(token_payload["access_token"])
    except google_oauth.OAuthError as exc:
        logger.warning("oauth leg failed", extra={"oauth_error_code": exc.code or "unknown"})
        audit.record(actor=None, action="auth.signin_denied", target_type="google", metadata={"reason": "upstream"})
        return _plain("Sign-in failed talking to Google. Try again.", 502)

    if not identity.email_verified:
        audit.record(actor=None, action="auth.signin_denied", target_type="google", metadata={"reason": "unverified_email"})
        return _plain("Sign-in failed: your Google email is not verified.", 403)

    user = signin.session_user(request.session.get(SESSION_USER_KEY)) if reconnecting else None
    if reconnecting and user is None:
        # the session claims a user the database does not have: a deleted account
        # with a live cookie. There is nobody to reconnect for.
        audit.record(actor=None, action="auth.signin_denied", target_type="user", metadata={"reason": "no_session"})
        return _plain("Reconnect failed: sign in again first.", 403)

    if user is None:
        user = signin.resolve_identity(identity)
        if user is None:
            audit.record(actor=None, action="auth.signin_denied", target_type="google", metadata={"reason": "not_permitted"})
            return _plain("This Google account is not permitted to sign in.", 403)
    elif identity.sub != user.google_sub:
        # reconnecting must never swap which account the session points at
        audit.record(actor=user, action="auth.signin_denied", target_type="google", metadata={"reason": "identity_mismatch"})
        return _plain("Reconnect failed: that Google account is not the signed-in one.", 403)

    try:
        stored = token_store.store_tokens(user, token_payload)
    except crypto.TokenKeyError:
        # a key problem is ours, not the user's: refuse loudly, do not sign in
        # half-configured
        logger.error("token storage refused: unusable OPSHUB_TOKEN_KEY")
        return _plain("Sign-in failed: token storage is misconfigured on this server.", 500)

    audit.record(
        actor=user,
        action="auth.tokens_stored",
        target_type="user",
        target_id=user.pk,
        metadata={"refresh_token_rotated": stored["refresh_token"]},
    )
    if reconnecting:
        # store_tokens already flipped the stopped sources back to active and
        # restamped refresh_token_obtained_at, because Google only returns a
        # refresh token after a fresh consent. The session keeps its key: no
        # privilege changed, so there is nothing to cycle away from.
        audit.record(actor=user, action="auth.reconnect", target_type="user", target_id=user.pk)
        return redirect("/", permanent=False)

    request.session.cycle_key()  # no session fixation across a privilege change
    request.session[SESSION_USER_KEY] = user.pk
    audit.record(actor=user, action="auth.signin", target_type="user", target_id=user.pk)
    return redirect("/", permanent=False)