"""Sync the calendar sources that are due.

The command is the loop's body: pick due sources, refresh the user's token,
fetch a window, write it. `--dry-run` stops after the first step, which is what
makes it safe to run against production while watching what it would touch.

Failure is per source, not per run. One user's revoked grant must not cost the
other four their sync, so each source is its own try/except and the summary
reports per-source outcomes at the end.

The Google client is built by `build_service`, which is imported lazily. A run
that never reaches a client — a dry run, or a process with no Google credentials
configured — should not fail on an import it does not need.
"""

from __future__ import annotations

import datetime
import logging
from urllib.parse import quote, urlencode

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.services import google_oauth, refresh
from accounts.services.tokens import read_access_token
from calendar_app.google_sync import SyncOutcome, sync_calendar
from core.scheduler import due_sources

logger = logging.getLogger(__name__)

# The window a sync keeps current: four weeks back so a meeting created late in
# the past still lands, four weeks forward so the board's horizon is not a
# cliff. §5.1 requires every stored row in UTC, so the bounds are UTC too.
WINDOW_PAST_DAYS = 28
WINDOW_FUTURE_DAYS = 28


def build_service(access_token: str | None):
    """A Calendar client with the one method google_sync calls.

    A missing token is a programming error at this point — the caller has
    already run the refresh — so it is refused rather than turned into an
    unauthenticated request that would fail confusingly further in.

    Built on `accounts.services.google_oauth._http` rather than
    googleapiclient, for one reason: that module is already this project's one
    place that touches the network, and it already turns a Google error body
    into a coarse code. A second HTTP path would be a second place to get
    redaction and error-code translation wrong.
    """
    if not access_token:
        raise ValueError("no access token for this user; run refresh_if_needed first")
    base = "https://www.googleapis.com/calendar/v3"

    class _Events:
        def list_events(self, *, calendar_id, time_min, time_max, sync_token, page_token):
            params = {
                "timeMin": time_min.isoformat(),
                "timeMax": time_max.isoformat(),
                # recurring instances arrive expanded, which is what the
                # availability algorithm wants: it merges blocks, not rules
                "singleEvents": True,
                "maxResults": 250,
            }
            if sync_token:
                params["syncToken"] = sync_token
            if page_token:
                params["pageToken"] = page_token
            url = f"{base}/calendars/{quote(calendar_id, safe='')}/events?{urlencode(params)}"
            return google_oauth._http("GET", url, access_token=access_token)

    return _Events()


class Command(BaseCommand):
    help = "Sync due calendar sources from Google."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List the sources that would be synced and stop.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        rows = due_sources(now=now).filter(kind="calendar").select_related("user")

        if options["dry_run"]:
            self.stdout.write(f"would sync {rows.count()} calendar sources")
            for source in rows:
                self.stdout.write(f"  user={source.user_id} cursor={source.cursor or '-'}")
            return None

        window_start = now - datetime.timedelta(days=WINDOW_PAST_DAYS)
        window_end = now + datetime.timedelta(days=WINDOW_FUTURE_DAYS)

        counts = {SyncOutcome.SYNCED: 0, SyncOutcome.FAILED: 0, refresh.OUTCOME_NEEDS_REAUTH: 0}
        for source in rows:
            outcome = refresh.refresh_if_needed(source.user, now=now)
            if outcome != refresh.OUTCOME_REFRESHED and outcome != refresh.OUTCOME_SKIPPED:
                # needs_reauth already marked the source; deferred already set
                # next_attempt_at. Either way, not our work this tick.
                counts[outcome] = counts.get(outcome, 0) + 1
                self.stdout.write(f"  user={source.user_id} {outcome}")
                continue

            try:
                service = build_service(read_access_token(source.user))
                result = sync_calendar(
                    source, service, window_start=window_start, window_end=window_end, now=now
                )
            except Exception:
                logger.warning(
                    "calendar sync raised",
                    extra={"app_user_id": source.user_id, "calendar_error": "unhandled"},
                )
                result = SyncOutcome.FAILED

            counts[result] = counts.get(result, 0) + 1
            self.stdout.write(f"  user={source.user_id} {result.value}")

        summary = " ".join(f"{key.value}={value}" for key, value in counts.items() if value)
        self.stdout.write(f"calendar sync: {summary or 'nothing due'}")
        return None