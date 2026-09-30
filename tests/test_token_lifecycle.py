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


# ------------------------------------------------- step 13: Testing mode
#
# A Google OAuth app in "Testing" publishing status hands out refresh tokens
# that live 7 days. That is this build's permanent operating condition, so the
# five behaviors below are not error handling for an edge case — they are the
# weekly rhythm. Every test name states the behavior it proves, so a red test
# names the missing behavior instead of a function that stopped working.

import io

from django.core.management import call_command
from django.template.loader import render_to_string

from accounts import oauth
from accounts.services.google_oauth import Identity
from accounts.views import RECONNECT_SESSION_KEY, SESSION_USER_KEY
from audit.models import AuditLog
from core.scheduler import due_sources


def _aged_user(db, *, days, email, sub):
    """A user whose stored refresh token was obtained `days` ago."""
    user = AppUser.objects.create(email=email, display_name="Aged", google_sub=sub)
    token_store.store_tokens(
        user,
        {"access_token": "ya29.old", "refresh_token": f"1//{sub}"},
        now=timezone.now() - timedelta(days=days),
    )
    for kind in ("gmail", "calendar"):
        Source.objects.get_or_create(user=user, kind=kind)
    user.refresh_from_db()
    return user


def _stopped(user, status=Source.Status.NEEDS_REAUTH, code="invalid_grant"):
    """What an invalid_grant leaves behind: stopped, errored, unscheduled."""
    Source.objects.filter(user=user).update(
        status=status, last_error_code=code, disconnected_at=timezone.now(), next_attempt_at=None
    )
    user.refresh_from_db()
    return user


def _die_with_invalid_grant(user):
    """One refresh attempt that Google refuses. Returns the outcome."""
    slept, _ = _slept()
    with mock.patch("accounts.services.google_oauth._http", side_effect=OAuthError("bad", code="invalid_grant")):
        return refresh_service.refresh_if_needed(user, now=timezone.now(), sleep=slept.append)


# --- behavior 1: the sweep names who is about to be hit, before the failure


def test_sweep_expiring_tokens_flags_active_sources_past_the_seven_day_warning_window(db):
    now = timezone.now()
    young = _aged_user(db, days=1, email="young@example.com", sub="sub-young")
    entering = _aged_user(db, days=6, email="entering@example.com", sub="sub-entering")
    overdue = _aged_user(db, days=9, email="overdue@example.com", sub="sub-overdue")

    swept = refresh_service.sweep_expiring_tokens(now=now)
    flagged = {user.email: cause for user, cause in swept}

    assert young.email not in flagged, "a token with six days of life left is not expiring"
    assert entering.email in flagged, "day 6 is inside the 2-day warning window"
    assert overdue.email in flagged
    assert len(swept) == 2, "one entry per user, not one per source"
    assert flagged[overdue.email] == "expiry"


def test_sweep_expiring_tokens_skips_a_source_that_is_not_active(db):
    stopped = _stopped(_aged_user(db, days=9, email="stopped@example.com", sub="sub-stopped"))
    gone = _aged_user(db, days=9, email="gone@example.com", sub="sub-gone")
    Source.objects.filter(user=gone).update(status=Source.Status.DISCONNECTED)
    live = _aged_user(db, days=9, email="live@example.com", sub="sub-live")

    swept = refresh_service.sweep_expiring_tokens(now=timezone.now())

    assert [user.email for user, _ in swept] == [live.email]
    assert stopped.pk not in [user.pk for user, _ in swept], "a stopped user already has the banner"
    assert gone.pk not in [user.pk for user, _ in swept], "a disconnected user asked to be left alone"


# --- behavior 2: a grant that dies past 7 days is an expiry, and says so


def test_seven_day_expiry_banner_names_the_testing_mode_cause_not_a_generic_error(db, settings):
    settings.GOOGLE_CLIENT_ID = "client-id"
    settings.GOOGLE_CLIENT_SECRET = "secret"
    user = _aged_user(db, days=8, email="expired@example.com", sub="sub-expired")

    assert _die_with_invalid_grant(user) == refresh_service.OUTCOME_NEEDS_REAUTH
    for source in user.sources.all():
        assert source.status == Source.Status.NEEDS_REAUTH
        assert source.last_error_code == "invalid_grant"

    banner = refresh_service.banner_for(user)
    assert banner["kind"] == "expiry"
    assert "7 days" in banner["message"]
    assert "testing mode" in banner["message"].lower()
    assert "revoked" not in banner["message"].lower(), "an expiry must not be reported as a revocation"


def test_seven_day_expiry_banner_renders_a_reconnect_action(db):
    user = _stopped(_aged_user(db, days=8, email="rendered@example.com", sub="sub-rendered"))

    html = render_to_string("partials/reconnect_banner.html", {"banner": refresh_service.banner_for(user)})

    assert "reconnect-banner--expiry" in html
    assert 'href="/reconnect"' in html
    assert "7 days" in html


# --- behavior 3: a grant that dies young is a revocation, not an expiry


def test_invalid_grant_under_seven_days_is_reported_as_a_revocation(db, settings):
    settings.GOOGLE_CLIENT_ID = "client-id"
    settings.GOOGLE_CLIENT_SECRET = "secret"
    user = _aged_user(db, days=2, email="revoked@example.com", sub="sub-revoked")

    assert _die_with_invalid_grant(user) == refresh_service.OUTCOME_NEEDS_REAUTH
    for source in user.sources.all():
        assert source.status == Source.Status.NEEDS_REAUTH

    banner = refresh_service.banner_for(user)
    assert banner["kind"] == "revocation"
    assert "revoked" in banner["message"].lower()


def test_revocation_banner_differs_from_the_seven_day_expiry_banner(db):
    expired = _stopped(_aged_user(db, days=8, email="exp@example.com", sub="sub-exp"))
    revoked = _stopped(_aged_user(db, days=2, email="rev@example.com", sub="sub-rev"))

    expiry_banner = refresh_service.banner_for(expired)
    revocation_banner = refresh_service.banner_for(revoked)

    assert expiry_banner["kind"] == "expiry"
    assert revocation_banner["kind"] == "revocation"
    assert expiry_banner["message"] != revocation_banner["message"]
    assert "7 days" not in revocation_banner["message"], "a young-token revocation is not a 7-day expiry"
    assert expiry_banner["show_action"] is revocation_banner["show_action"] is True


# --- behavior 4: a disconnected user costs zero API calls


def test_scheduler_query_skips_non_active_sources_so_a_disconnected_user_costs_zero_calls(db):
    live = _aged_user(db, days=1, email="due@example.com", sub="sub-due")
    stopped = _stopped(_aged_user(db, days=1, email="halted@example.com", sub="sub-halted"))
    gone = _aged_user(db, days=1, email="disco@example.com", sub="sub-disco")
    Source.objects.filter(user=gone).update(status=Source.Status.DISCONNECTED)
    errored = _aged_user(db, days=1, email="err@example.com", sub="sub-err")
    Source.objects.filter(user=errored).update(status=Source.Status.ERROR, last_error_code="network")

    selected = set(due_sources().values_list("user_id", flat=True).distinct())

    assert selected == {live.pk}, "only active sources are handed to a sync worker"
    assert {stopped.pk, gone.pk, errored.pk}.isdisjoint(selected)


def test_sync_calendar_command_skips_a_disconnected_users_source(db):
    live = _aged_user(db, days=1, email="cal@example.com", sub="sub-cal")
    gone = _aged_user(db, days=1, email="calgone@example.com", sub="sub-calgone")
    Source.objects.filter(user=gone).update(status=Source.Status.DISCONNECTED)

    out = io.StringIO()
    call_command("sync_calendar", kind="calendar", stdout=out)
    text = out.getvalue()

    assert "due calendar sources: 1" in text
    assert f"user={live.pk}" in text
    assert f"user={gone.pk}" not in text


# --- behavior 5: /reconnect brings the same user back, connected


def test_reconnect_start_marks_the_session_and_asks_google_for_consent(client, settings, db):
    settings.GOOGLE_CLIENT_ID = "client-id.apps.googleusercontent.com"
    settings.GOOGLE_REDIRECT_URI = "/auth/callback"
    user = _stopped(_aged_user(db, days=9, email="recon@example.com", sub="sub-recon"))

    # the gate that keeps the flow signed-in: a stranger gets sent to sign in
    assert client.get("/reconnect")["Location"] == "/auth/google"

    session = client.session
    session[SESSION_USER_KEY] = user.pk
    session.save()

    response = client.get("/reconnect")

    assert response.status_code == 302
    assert response["Location"].startswith("https://accounts.google.com/")
    assert "prompt=consent" in response["Location"], "consent is what mints a new refresh token"
    assert "access_type=offline" in response["Location"]
    assert client.session[RECONNECT_SESSION_KEY] is True
    assert oauth.STATE_SESSION_KEY in client.session
    assert user.sources.filter(status=Source.Status.ACTIVE).count() == 0, "nothing is active before consent"


def test_reconnect_callback_restores_active_and_refuses_another_google_account(client, settings, db):
    settings.GOOGLE_CLIENT_ID = "client-id.apps.googleusercontent.com"
    settings.GOOGLE_CLIENT_SECRET = "secret"
    settings.GOOGLE_REDIRECT_URI = "/auth/callback"
    user = _stopped(_aged_user(db, days=9, email="back@example.com", sub="sub-back"))
    old_obtained = user.refresh_token_obtained_at

    session = client.session
    session[SESSION_USER_KEY] = user.pk
    session.save()
    client.get("/reconnect")
    state = client.session[oauth.STATE_SESSION_KEY]

    mine = Identity(sub="sub-back", email="back@example.com", email_verified=True)
    with mock.patch(
        "accounts.services.google_oauth._http",
        return_value={"access_token": "ya29.fresh", "refresh_token": "1//brand-new", "expires_in": 3600},
    ) as http, mock.patch("accounts.services.google_oauth.fetch_identity", return_value=mine):
        response = client.get("/auth/callback", {"code": "auth-code", "state": state})

    assert response.status_code == 302
    assert response["Location"] == "/"
    assert http.call_args.kwargs["data"]["grant_type"] == "authorization_code", "the stdlib exchange path, unchanged"
    user.refresh_from_db()
    assert user.refresh_token_obtained_at > old_obtained, "a fresh grant restarts the 7-day clock"
    assert token_store.read_refresh_token(user) == "1//brand-new"
    for source in user.sources.all():
        assert source.status == Source.Status.ACTIVE
        assert source.last_error_code is None
        assert source.disconnected_at is None
    assert client.session[SESSION_USER_KEY] == user.pk
    assert RECONNECT_SESSION_KEY not in client.session, "one round trip, one intent"
    assert AuditLog.objects.filter(action="auth.reconnect", actor=user).exists()

    # the same flow driven with somebody else's Google account must change nothing
    stolen = Identity(sub="sub-someone-else", email="attacker@example.com", email_verified=True)
    client.get("/reconnect")
    state = client.session[oauth.STATE_SESSION_KEY]
    before = user.refresh_token_obtained_at
    with mock.patch(
        "accounts.services.google_oauth._http",
        return_value={"access_token": "ya29.stolen", "refresh_token": "1//stolen"},
    ) as http, mock.patch("accounts.services.google_oauth.fetch_identity", return_value=stolen):
        refused = client.get("/auth/callback", {"code": "auth-code", "state": state})

    assert refused.status_code == 403
    assert b"not the signed-in one" in refused.content
    user.refresh_from_db()
    assert user.refresh_token_obtained_at == before
    assert token_store.read_refresh_token(user) == "1//brand-new", "a refused identity stores no token"
    assert not AppUser.objects.filter(email="attacker@example.com").exists()
