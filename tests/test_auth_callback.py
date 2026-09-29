"""The callback leg. Everything hostile happens here: a forged state, a replayed
callback, an unverified Google address, a Google outage. The tests below drive
the real view and patch only the two HTTPS primitives, so the state machine in
the view is what is under test.
"""

from unittest import mock

import pytest
from django.urls import reverse

from accounts import oauth
from accounts.services.google_oauth import Identity
from accounts.views import SESSION_USER_KEY
from audit.models import AuditLog

CALLBACK = "/auth/callback"

VERIFIED = Identity(sub="sub-1", email="user@example.com", email_verified=True, name="User One")


@pytest.fixture
def configured(settings):
    settings.GOOGLE_CLIENT_ID = "client-id.apps.googleusercontent.com"
    settings.GOOGLE_CLIENT_SECRET = "secret"
    settings.GOOGLE_REDIRECT_URI = CALLBACK
    # the sign-in gate is fail-closed, so a successful callback must name a domain
    settings.ALLOWED_EMAIL_DOMAINS = ["example.com"]
    return settings


def _prime(client):
    """Run the start leg so the session holds a real state and verifier."""
    client.get("/auth/google")
    return client.session[oauth.STATE_SESSION_KEY]


def _stub(token_payload=None, identity=VERIFIED):
    token_payload = token_payload or {"access_token": "ya29.access", "refresh_token": "1//refresh"}
    return (
        mock.patch("accounts.services.google_oauth._http", return_value=token_payload),
        mock.patch("accounts.services.google_oauth.fetch_identity", return_value=identity),
    )


def _callback(client, **params):
    from urllib.parse import urlencode

    return client.get(CALLBACK, params)


def test_callback_succeeds_for_verified_email_and_consumes_state(client, configured, db):
    state = _prime(client)
    token_stub, identity_stub = _stub()
    with token_stub, identity_stub:
        response = _callback(client, code="auth-code", state=state)
    assert response.status_code == 302
    assert response["Location"] == "/"

    from accounts.models import AppUser

    user = AppUser.objects.get(google_sub=VERIFIED.sub)
    assert user.email == "user@example.com"
    assert client.session[SESSION_USER_KEY] == user.pk
    # one attempt, one nonce: a replayed callback has nothing left to match
    assert oauth.STATE_SESSION_KEY not in client.session
    assert oauth.VERIFIER_SESSION_KEY not in client.session
    assert AuditLog.objects.filter(action="auth.signin", actor=user).exists()

    token_stub, identity_stub = _stub()
    with token_stub, identity_stub:
        assert _callback(client, code="auth-code", state=state).status_code == 400


def test_callback_rejects_state_mismatch(client, configured, db):
    _prime(client)
    token_stub, identity_stub = _stub()
    with token_stub, identity_stub:
        response = _callback(client, code="auth-code", state=oauth.new_state())
    assert response.status_code == 400
    assert b"state mismatch" in response.content
    assert SESSION_USER_KEY not in client.session
    assert AuditLog.objects.filter(action="auth.signin_denied", metadata__reason="state_mismatch").exists()
    assert not AuditLog.objects.filter(action="auth.signin").exists()


def test_callback_rejects_state_mismatch_when_session_is_empty(client, configured, db):
    token_stub, identity_stub = _stub()
    with token_stub, identity_stub:
        response = _callback(client, code="auth-code", state=oauth.new_state())
    assert response.status_code == 400
    assert SESSION_USER_KEY not in client.session


def test_callback_rejects_unverified_email(client, configured, db):
    state = _prime(client)
    unverified = Identity(sub="sub-2", email="mallory@example.com", email_verified=False, name="M")
    token_stub, identity_stub = _stub(identity=unverified)
    with token_stub, identity_stub:
        response = _callback(client, code="auth-code", state=state)
    assert response.status_code == 403
    assert b"not verified" in response.content
    assert SESSION_USER_KEY not in client.session

    from accounts.models import AppUser

    assert not AppUser.objects.filter(email="mallory@example.com").exists()
    assert AuditLog.objects.filter(action="auth.signin_denied", metadata__reason="unverified_email").exists()


def test_callback_reports_provider_error_without_exchanging(client, configured, db):
    _prime(client)
    response = _callback(client, error="access_denied")
    assert response.status_code == 400
    assert b"cancelled or denied" in response.content
    assert SESSION_USER_KEY not in client.session
    assert AuditLog.objects.filter(action="auth.signin_denied", metadata__reason="provider_error").exists()


def test_callback_requires_code_and_survives_google_outage(client, configured, db):
    from accounts.services import google_oauth

    state = _prime(client)
    assert _callback(client, state=state).status_code == 400  # no code

    state = _prime(client)
    with mock.patch("accounts.services.google_oauth._http", side_effect=google_oauth.OAuthError("down", code="network")):
        response = _callback(client, code="auth-code", state=state)
    assert response.status_code == 502
    assert SESSION_USER_KEY not in client.session
    assert AuditLog.objects.filter(action="auth.signin_denied", metadata__reason="upstream").exists()