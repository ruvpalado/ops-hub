"""Redaction: secrets never reach the logs. Covers the token lifecycle and the
OAuth handshake, the two places with something worth leaking."""

import logging

from core.redaction import redact, RedactionFilter


def test_redaction_access_token_value():
    line = 'POST /oauth2/v4/token 200 218ms body access_token=ya29.A0b1C2D3e4 client_id=ok'
    out = redact(line)
    assert "ya29.A0b1C2D3e4" not in out
    assert "access_token=[redacted]" in out


def test_redaction_bearer_header():
    line = "GET /gmail/v1/users/me/messages 200 41ms 'Authorization': 'Bearer ya29.zz99xx88AA'"
    out = redact(line)
    assert "ya29.zz99xx88AA" not in out
    assert "[redacted]" in out


def test_redaction_refresh_and_client_secret():
    line = '{"refresh_token":"1//0abcDEF","client_secret":"GOCSPX-aaaabbbbcccc"}'
    out = redact(line)
    assert "1//0abcDEF" not in out
    assert "GOCSPX-aaaabbbbcccc" not in out
    assert "refresh_token" in out
    assert "client_secret" in out
    assert "[redacted]" in out


def test_redaction_logging_filter_scrubs_records(caplog):
    logger = logging.getLogger("opshub.token_lifecycle_test")
    logger.addFilter(RedactionFilter())
    with caplog.at_level(logging.INFO, logger="opshub.token_lifecycle_test"):
        logger.info("refresh ok access_token=%s", "ya29.xXxXxXxXxXxXxXx")
        logger.info("code=%s", "4/0Aabc_DEfg-hIjK")
    out = caplog.text
    assert "ya29.xXxXxXxXxXxXxXx" not in out
    assert "4/0Aabc_DEfg-hIjK" not in out
    assert "access_token=[redacted]" in out
    assert "code=[redacted]" in out

# ---------------------------------------------------------------- step 11: storage

from datetime import timedelta

from django.utils import timezone

from accounts import crypto
from accounts.models import AppUser
from accounts.services import tokens as token_store
from core.models import Source


def _user(db):
    """A user with both source slots, exactly as sign-in would leave them."""
    user = AppUser.objects.create(email="tok@example.com", display_name="Tok", google_sub="sub-t")
    Source.objects.get_or_create(user=user, kind="gmail")
    Source.objects.get_or_create(user=user, kind="calendar")
    return user


def test_store_tokens_writes_encrypted_columns_and_expiry(db):
    user = _user(db)
    now = timezone.now()

    changed = token_store.store_tokens(
        user, {"access_token": "ya29.access-1", "refresh_token": "1//refresh-1", "expires_in": 3600}, now=now
    )

    user.refresh_from_db()
    assert user.access_token and user.refresh_token
    assert b"ya29.access-1" not in bytes(user.access_token), "token must not sit in the column in the clear"
    assert b"1//refresh-1" not in bytes(user.refresh_token)
    assert crypto.decrypt(bytes(user.access_token)) == b"ya29.access-1"
    assert crypto.decrypt(bytes(user.refresh_token)) == b"1//refresh-1"
    assert user.token_expires_at == now + timedelta(seconds=3600)
    assert user.refresh_token_obtained_at == now
    assert changed == {"access_token": True, "refresh_token": True, "token_expires_at": now + timedelta(seconds=3600)}


def test_store_tokens_preserves_refresh_token_when_google_omits_it(db):
    user = _user(db)
    first_at = timezone.now() - timedelta(days=3)
    token_store.store_tokens(
        user, {"access_token": "ya29.old", "refresh_token": "1//keep-me", "expires_in": 3600}, now=first_at
    )
    user.refresh_from_db()
    stored_blob, stored_at = bytes(user.refresh_token), user.refresh_token_obtained_at

    later = timezone.now()
    changed = token_store.store_tokens(user, {"access_token": "ya29.new", "expires_in": 3600}, now=later)
    user.refresh_from_db()

    assert bytes(user.refresh_token) == stored_blob
    assert crypto.decrypt(bytes(user.refresh_token)) == b"1//keep-me"
    assert user.refresh_token_obtained_at == stored_at == first_at
    assert user.refresh_token_obtained_at != later
    assert changed["refresh_token"] is False
    assert crypto.decrypt(bytes(user.access_token)) == b"ya29.new"


def test_store_tokens_restarts_obtained_at_only_for_a_new_refresh_token(db):
    user = _user(db)
    token_store.store_tokens(user, {"access_token": "a", "refresh_token": "1//first"}, now=timezone.now())
    user.refresh_from_db()
    first_at = user.refresh_token_obtained_at

    token_store.store_tokens(user, {"access_token": "b", "refresh_token": "1//second"}, now=timezone.now())
    user.refresh_from_db()
    assert user.refresh_token_obtained_at > first_at
    assert crypto.decrypt(bytes(user.refresh_token)) == b"1//second"


def test_store_tokens_defaults_missing_expires_in_to_an_hour(db):
    user = _user(db)
    now = timezone.now()
    token_store.store_tokens(user, {"access_token": "a", "refresh_token": "r"}, now=now)

    user.refresh_from_db()
    assert user.token_expires_at == now + timedelta(seconds=token_store.DEFAULT_EXPIRES_IN)


def test_store_tokens_reactivates_sources_but_not_disconnected_ones(db):
    user = _user(db)
    signin_sources = [Source.objects.get_or_create(user=user, kind=k)[0] for k in ("gmail", "calendar")]
    gmail, calendar = signin_sources
    calendar.status = Source.Status.DISCONNECTED
    calendar.disconnected_at = timezone.now()
    calendar.save()
    gmail.status = Source.Status.NEEDS_REAUTH
    gmail.last_error_code = "invalid_grant"
    gmail.disconnected_at = timezone.now()
    gmail.save()

    token_store.store_tokens(user, {"access_token": "a", "refresh_token": "r"}, now=timezone.now())

    gmail.refresh_from_db()
    calendar.refresh_from_db()
    assert gmail.status == Source.Status.ACTIVE
    assert gmail.last_error_code is None
    assert gmail.disconnected_at is None
    assert calendar.status == Source.Status.DISCONNECTED
    assert calendar.disconnected_at is not None


# ------------------------------------------------------------- step 12: refresh

from unittest import mock

from accounts.services import refresh as refresh_service
from accounts.services.google_oauth import OAuthError


def _ready_user(db, *, expires_in=30):
    """A user whose access token is inside the 60s margin, so a refresh is due."""
    user = _user(db)
    now = timezone.now()
    token_store.store_tokens(
        user, {"access_token": "ya29.old", "refresh_token": "1//keep-me", "expires_in": expires_in}, now=now
    )
    user.refresh_from_db()
    Source.objects.filter(user=user).update(
        cursor="cursor-abc", last_synced_at=now - timedelta(hours=1)
    )
    return user


def _slept():
    recorded = []
    return recorded, recorded.append


def test_refresh_skips_the_call_when_the_token_is_comfortably_valid(db):
    user = _user(db)
    now = timezone.now()
    token_store.store_tokens(user, {"access_token": "ya29.fresh", "refresh_token": "1//r"}, now=now)
    user.refresh_from_db()
    before = bytes(user.access_token)

    with mock.patch("accounts.services.google_oauth._http") as http:
        outcome = refresh_service.refresh_if_needed(user, now=now)

    assert outcome == refresh_service.OUTCOME_SKIPPED
    assert http.call_count == 0
    user.refresh_from_db()
    assert bytes(user.access_token) == before


def test_refresh_swaps_the_access_token_and_keeps_the_refresh_token(db, settings):
    settings.GOOGLE_CLIENT_ID = "client-id"
    settings.GOOGLE_CLIENT_SECRET = "secret"
    user = _ready_user(db)
    user.refresh_from_db()
    stored_refresh_blob, stored_obtained = bytes(user.refresh_token), user.refresh_token_obtained_at
    now = timezone.now()
    slept, _ = _slept()

    with mock.patch(
        "accounts.services.google_oauth._http", return_value={"access_token": "ya29.new", "expires_in": 3600}
    ) as http:
        outcome = refresh_service.refresh_if_needed(user, now=now, sleep=slept.append)

    assert outcome == refresh_service.OUTCOME_REFRESHED
    assert slept == [], "a successful refresh must not sleep"
    assert http.call_args.kwargs["data"]["grant_type"] == "refresh_token"
    assert http.call_args.kwargs["data"]["refresh_token"] == "1//keep-me"
    user.refresh_from_db()
    assert token_store.read_access_token(user) == "ya29.new"
    assert user.token_expires_at == now + timedelta(seconds=3600)
    assert bytes(user.refresh_token) == stored_refresh_blob
    assert user.refresh_token_obtained_at == stored_obtained


def test_refresh_invalid_grant_needs_reauth_and_leaves_data_byte_identical(db, settings):
    settings.GOOGLE_CLIENT_ID = "client-id"
    settings.GOOGLE_CLIENT_SECRET = "secret"
    user = _ready_user(db)
    now = timezone.now()

    def snapshot():
        from accounts.models import AppUser

        row = AppUser.objects.get(pk=user.pk)
        return {
            "email": row.email,
            "display_name": row.display_name,
            "google_sub": row.google_sub,
            "role": row.role,
            "timezone": row.timezone,
            "refresh_token": bytes(row.refresh_token),
            "access_token": bytes(row.access_token),
            "refresh_token_obtained_at": row.refresh_token_obtained_at,
            "sources": sorted(
                (s.kind, s.cursor, s.last_synced_at, s.created_at) for s in row.sources.all()
            ),
        }

    before = snapshot()
    slept, _ = _slept()

    with mock.patch("accounts.services.google_oauth._http", side_effect=OAuthError("bad", code="invalid_grant")):
        outcome = refresh_service.refresh_if_needed(user, now=now, sleep=slept.append)

    assert outcome == refresh_service.OUTCOME_NEEDS_REAUTH
    assert slept == [], "invalid_grant is terminal; retrying would be wrong"
    assert snapshot() == before, "no user or sync data may change on invalid_grant"

    for source in user.sources.all():
        assert source.status == Source.Status.NEEDS_REAUTH
        assert source.last_error_code == "invalid_grant"
        assert source.disconnected_at == now
        assert source.next_attempt_at is None, "needs_reauth rows must never be auto-retried"


def test_refresh_defers_with_backoff_on_transient_failure(db, settings):
    settings.GOOGLE_CLIENT_ID = "client-id"
    settings.GOOGLE_CLIENT_SECRET = "secret"
    user = _ready_user(db)
    now = timezone.now()
    slept, _ = _slept()

    with mock.patch(
        "accounts.services.google_oauth._http", side_effect=OAuthError("boom", code="http_503")
    ) as http:
        outcome = refresh_service.refresh_if_needed(user, now=now, sleep=slept.append)

    assert outcome == refresh_service.OUTCOME_DEFERRED
    assert slept == list(refresh_service.RETRY_DELAYS_SECONDS) == [2, 8]
    assert http.call_count == 3, "one attempt plus two retries"
    assert user.sources.count() == 2
    for source in user.sources.all():
        assert source.status == Source.Status.ACTIVE, "a flaky network must not disconnect anyone"
        assert source.last_error_code == "upstream_5xx"
        assert source.next_attempt_at == now + timedelta(minutes=refresh_service.DEFER_MINUTES)
        assert source.disconnected_at is None


def test_refresh_without_a_stored_refresh_token_needs_reauth_without_a_call(db):
    user = _user(db)
    now = timezone.now()

    with mock.patch("accounts.services.google_oauth._http") as http:
        outcome = refresh_service.refresh_if_needed(user, now=now)

    assert outcome == refresh_service.OUTCOME_NEEDS_REAUTH
    assert http.call_count == 0, "there is nothing to present to Google"
    assert user.sources.count() == 2
    for source in user.sources.all():
        assert source.status == Source.Status.NEEDS_REAUTH
        assert source.last_error_code == "no_token"
    assert source.next_attempt_at is None


def test_refresh_network_failure_defers_with_the_network_error_code(db, settings):
    settings.GOOGLE_CLIENT_ID = "client-id"
    settings.GOOGLE_CLIENT_SECRET = "secret"
    user = _ready_user(db)
    now = timezone.now()
    slept, _ = _slept()

    with mock.patch("accounts.services.google_oauth._http", side_effect=OAuthError("unreachable", code="network")):
        outcome = refresh_service.refresh_if_needed(user, now=now, sleep=slept.append)

    assert outcome == refresh_service.OUTCOME_DEFERRED
    assert slept == [2, 8]
    assert user.sources.count() == 2
    for source in user.sources.all():
        assert source.status == Source.Status.ACTIVE
        assert source.last_error_code == "network"
        assert source.next_attempt_at == now + timedelta(minutes=15)
        assert source.disconnected_at is None
