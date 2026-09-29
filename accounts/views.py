"""Views for the Google sign-in handshake. No ORM queries live here: a view
builds a request, hands it to a service or to accounts.oauth, and returns a
response. The session is the only state a view touches.
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponseRedirect
from django.views.decorators.http import require_GET

from accounts import oauth


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