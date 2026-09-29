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

logger = logging.getLogger(__name__)

SESSION_USER_KEY = "app_user_id"


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

    user = signin.resolve_identity(identity)
    if user is None:
        audit.record(actor=None, action="auth.signin_denied", target_type="google", metadata={"reason": "not_permitted"})
        return _plain("This Google account is not permitted to sign in.", 403)

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
    request.session.cycle_key()  # no session fixation across a privilege change
    request.session[SESSION_USER_KEY] = user.pk
    audit.record(actor=user, action="auth.signin", target_type="user", target_id=user.pk)
    return redirect("/", permanent=False)