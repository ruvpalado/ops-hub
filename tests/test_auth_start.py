"""The start leg of the Google handshake: nonce quality, PKCE, scope discipline."""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from django.urls import reverse

from accounts import oauth

CLIENT_ID = "client-id.apps.googleusercontent.com"
REDIRECT_URI = "http://localhost:8000/auth/callback"


@pytest.fixture
def configured(settings):
    settings.GOOGLE_CLIENT_ID = CLIENT_ID
    settings.GOOGLE_REDIRECT_URI = REDIRECT_URI
    return settings


def _params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def test_state_is_32_byte_csprng_and_unique():
    first, second = oauth.new_state(), oauth.new_state()
    assert first != second
    # token_urlsafe drops padding; recover the byte count from the raw length
    raw = base64.urlsafe_b64decode(first + "=" * (-len(first) % 4))
    assert len(raw) == oauth.STATE_BYTES == 32
    assert oauth.state_matches(first, first) is True
    assert oauth.state_matches(first, second) is False
    assert oauth.state_matches(None, first) is False
    assert oauth.state_matches("", first) is False


def test_pkce_verifier_and_s256_challenge():
    verifier = oauth.new_code_verifier()
    assert 43 <= len(verifier) <= 128
    assert verifier != oauth.new_code_verifier()

    challenge = oauth.code_challenge(verifier)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode()
    assert challenge == expected.rstrip("=")
    assert "=" not in challenge and "+" not in challenge and "/" not in challenge

    params = _params(
        oauth.authorization_url(
            client_id=CLIENT_ID, redirect_uri=REDIRECT_URI, state="s", code_verifier=verifier
        )
    )
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"] == [challenge]


def test_scope_is_readonly_plus_send_only():
    params = _params(
        oauth.authorization_url(
            client_id=CLIENT_ID, redirect_uri=REDIRECT_URI, state="s", code_verifier="v" * 43
        )
    )
    scopes = params["scope"][0].split()
    assert scopes == ["gmail.readonly", "gmail.send", "calendar.readonly"]
    forbidden = {"gmail.modify", "gmail.compose", "calendar", "calendar.events", "drive", "userinfo.email"}
    assert forbidden.isdisjoint(set(scopes))
    # a full URL-scope (no .readonly suffix, no gmail prefix) is the classic mistake
    assert not any(s.count(".") == 0 for s in scopes)


def test_authorization_url_requests_offline_consent_and_rotates_state():
    url = oauth.authorization_url(
        client_id=CLIENT_ID, redirect_uri=REDIRECT_URI, state="s", code_verifier="v" * 43
    )
    params = _params(url)
    assert url.startswith(oauth.AUTHORIZATION_ENDPOINT + "?")
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["include_granted_scopes"] == ["false"]
    assert params["response_type"] == ["code"]
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["state"] == ["s"]


def test_start_view_redirects_and_stores_state_and_verifier_in_session(client, configured, db):
    response = client.get(reverse("auth_google_start"))
    assert response.status_code == 302

    params = _params(response["Location"])
    session_state = client.session[oauth.STATE_SESSION_KEY]
    verifier = client.session[oauth.VERIFIER_SESSION_KEY]

    assert params["state"] == [session_state]
    assert params["code_challenge"] == [oauth.code_challenge(verifier)]
    assert len(base64.urlsafe_b64decode(session_state + "=" * (-len(session_state) % 4))) == 32

    second = client.get(reverse("auth_google_start"))
    assert _params(second["Location"])["state"] != params["state"][0], "state must rotate per attempt"