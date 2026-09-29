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
    return AppUser.objects.create(email="tok@example.com", display_name="Tok", google_sub="sub-t")


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
