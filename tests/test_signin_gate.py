"""Invite-or-claim: who is allowed to turn a Google identity into an account.

Split by name on purpose — `-k invite` and `-k claim` are separate proofs of the
two halves, and a run that shows 4 and 4 rather than 8 tells the reader which
half broke.
"""

import pytest
from django.urls import reverse

from accounts.models import AppUser
from accounts.services.google_oauth import Identity
from accounts.services.signin import domain_allowed, resolve_identity
from accounts.views import SESSION_USER_KEY
from audit.models import AuditLog
from core.models import Source

ALLOWED = ["example.com"]


def claim_identity(**overrides):
    base = dict(sub="sub-1", email="user@example.com", email_verified=True, name="User One")
    return Identity(**{**base, **overrides})


# ---------------------------------------------------------------- invite half


def test_invite_placeholder_is_claimed_by_the_matching_verified_identity(db):
    invited = AppUser.objects.create(email="user@example.com", display_name="placeholder")
    assert invited.google_sub is None

    user = resolve_identity(claim_identity())

    assert user.pk == invited.pk
    user.refresh_from_db()
    assert user.google_sub == "sub-1"
    assert user.display_name == "User One"


def test_invite_placeholder_bound_to_another_sub_is_not_stolen(db):
    AppUser.objects.create(email="user@example.com", display_name="someone else", google_sub="sub-other")

    assert resolve_identity(claim_identity()) is None


def test_invited_account_keeps_one_row_across_repeat_sign_ins(db):
    invited = AppUser.objects.create(email="user@example.com", display_name="placeholder")

    first = resolve_identity(claim_identity())
    second = resolve_identity(claim_identity())

    assert first.pk == second.pk == invited.pk
    assert AppUser.objects.count() == 1
    assert Source.objects.filter(user=first).count() == 2


def test_invited_account_keeps_sources_after_workspace_email_rename(db):
    user = AppUser.objects.create(email="user@example.com", display_name="User One", google_sub="sub-1")
    resolve_identity(claim_identity(email="renamed@example.com", name="User Renamed"))

    assert AppUser.objects.count() == 1
    user.refresh_from_db()
    assert user.google_sub == "sub-1"
    assert user.display_name == "User Renamed"
    assert set(Source.objects.filter(user=user).values_list("kind", flat=True)) == {"gmail", "calendar"}


# ----------------------------------------------------------------- claim half


def test_claim_allowed_domain_creates_account_and_sources(settings, db):
    settings.ALLOWED_EMAIL_DOMAINS = ALLOWED

    user = resolve_identity(claim_identity())

    assert user.email == "user@example.com"
    assert user.google_sub == "sub-1"
    assert set(Source.objects.filter(user=user, status="active").values_list("kind", flat=True)) == {
        "gmail",
        "calendar",
    }


def test_claim_unlisted_domain_is_refused(settings, db):
    settings.ALLOWED_EMAIL_DOMAINS = ALLOWED

    assert resolve_identity(claim_identity(email="intruder@evil.test")) is None
    assert not AppUser.objects.filter(email="intruder@evil.test").exists()


def test_claim_fails_closed_when_allowlist_is_empty(settings, db):
    settings.ALLOWED_EMAIL_DOMAINS = []

    assert domain_allowed("user@example.com", []) is False
    assert resolve_identity(claim_identity()) is None
    assert AppUser.objects.count() == 0


def test_claim_match_is_exact_domain_not_a_suffix(settings, db):
    settings.ALLOWED_EMAIL_DOMAINS = ALLOWED

    assert domain_allowed("user@example.com", ALLOWED) is True
    assert domain_allowed("user@notexample.com", ALLOWED) is False
    assert domain_allowed("user@sub.example.com", ALLOWED) is False
    assert domain_allowed("user@EXAMPLE.com", ALLOWED) is True


def test_claim_refusal_is_visible_to_the_view_and_the_audit_trail(client, settings, db):
    from unittest import mock

    settings.ALLOWED_EMAIL_DOMAINS = ALLOWED
    settings.GOOGLE_CLIENT_ID = "client-id"
    settings.GOOGLE_CLIENT_SECRET = "secret"
    settings.GOOGLE_REDIRECT_URI = "http://testserver/auth/callback"
    client.get("/auth/google")
    from accounts import oauth

    state = client.session[oauth.STATE_SESSION_KEY]
    identity = claim_identity(email="intruder@evil.test")
    with mock.patch("accounts.services.google_oauth._http", return_value={"access_token": "a"}), mock.patch(
        "accounts.services.google_oauth.fetch_identity", return_value=identity
    ):
        response = client.get("/auth/callback", {"code": "c", "state": state})

    assert response.status_code == 403
    assert SESSION_USER_KEY not in client.session
    assert AuditLog.objects.filter(action="auth.signin_denied", metadata__reason="not_permitted").exists()
    assert reverse("home") == "/"
