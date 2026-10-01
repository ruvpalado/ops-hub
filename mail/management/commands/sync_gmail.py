"""Sync the Gmail sources that are due.

The command is the loop's body: pick due sources, refresh the user's token,
walk history, write the changed rows. `--dry-run` stops after the first step,
which is what makes it safe to run against production while watching what it
would touch.

Failure is per source, not per run. One user's revoked grant must not cost the
other four their sync, so each source is its own try/except and the summary
reports per-source outcomes at the end.

The Gmail client is built by `build_service`, imported lazily — a dry run or a
process with no Google credentials configured should not fail on an import it
does not need. It is built on `accounts.services.google_oauth._http` rather than
googleapiclient for the same reason the calendar command is: that module is
already this project's one network path, and it already turns a Google error
body into a coarse code. A second HTTP path would be a second place to get
redaction and error-code translation wrong.
"""

from __future__ import annotations

import logging
from urllib.parse import quote, urlencode

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.services import google_oauth, refresh
from accounts.services.tokens import read_access_token
from core.scheduler import due_sources
from mail import google_sync
from mail.google_sync import SyncOutcome, sync_gmail

logger = logging.getLogger(__name__)

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def build_service(access_token: str | None):
    """A Gmail client with the five methods google_sync calls.

    A missing token is a programming error at this point — the caller has
    already run the refresh — so it is refused rather than turned into an
    unauthenticated request that would fail confusingly further in.
    """
    if not access_token:
        raise ValueError("no access token for this user; run refresh_if_needed first")

    unread_label_id = None

    class _Gmail:
        @staticmethod
        def _call(path, *, method="GET", params=None, body=None):
            url = f"{GMAIL_BASE}{path}"
            if params:
                url = f"{url}?{urlencode(params)}"
            return google_oauth._http(method, url, data=body, access_token=access_token)

        def profile(self):
            return self._call("/profile")

        def history(self, *, start_history_id, page_token=None):
            params = {"startHistoryId": start_history_id, "historyTypes": "messageAdded"}
            if page_token:
                params["pageToken"] = page_token
            return self._call("/history", params=params)

        def messages_list(self, *, query, max_results):
            return self._call("/threads", params={"q": query, "maxResults": max_results})

        def get_thread(self, thread_id):
            return self._call(f"/threads/{quote(thread_id, safe='')}")

        def get_message(self, message_id, *, metadata_only=False):
            # format=metadata still returns the payload headers, which is all
            # normalize_message needs, and skips the base64 body decode — the
            # whole point of the flag in §6.2.
            fmt = "metadata" if metadata_only else "full"
            return self._call(
                f"/messages/{quote(message_id, safe='')}",
                params={"format": fmt, "metadataHeaders": "Subject"},
            )

        def labels_unread_id(self):
            """The UNREAD label id, fetched at most once per sync run.

            The spec allows two calls per sync for this; caching it on the client
            means a 250-message mailbox pays the same two as an empty one.
            """
            nonlocal unread_label_id
            if unread_label_id is None:
                payload = self._call("/labels/UNREAD")
                unread_label_id = payload["id"]
            return unread_label_id

    return _Gmail()


class Command(BaseCommand):
    help = "Sync due Gmail sources from Google."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List the sources that would be synced and stop.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        rows = due_sources(now=now).filter(kind="gmail").select_related("user")

        if options["dry_run"]:
            self.stdout.write(f"would sync {rows.count()} gmail sources")
            for source in rows:
                self.stdout.write(f"  user={source.user_id} cursor={source.cursor or '-'}")
            return None

        counts: dict = {}
        for source in rows:
            outcome = refresh.refresh_if_needed(source.user, now=now)
            if outcome not in (refresh.OUTCOME_REFRESHED, refresh.OUTCOME_SKIPPED):
                # needs_reauth already marked the source; deferred already set
                # next_attempt_at. Either way, not our work this tick.
                counts[outcome] = counts.get(outcome, 0) + 1
                self.stdout.write(f"  user={source.user_id} {outcome}")
                continue

            try:
                service = build_service(read_access_token(source.user))
                result = sync_gmail(source, service, now=now)
            except google_sync.RateLimited as exc:
                # The client raises on 429; sync_gmail handles its own, so this
                # only fires if the limiter is raised outside the sync's try.
                google_sync._defer(source, now, exc.retry_after_seconds)
                result = SyncOutcome.RATE_LIMITED
            except google_sync.HistoryExpired:
                source.cursor = None
                source.last_error_code = "sync_token_expired"
                source.save(update_fields=["cursor", "last_error_code", "updated_at"])
                result = SyncOutcome.HISTORY_EXPIRED
            except Exception:
                # Coarse code only, never the upstream body: §9.1.
                logger.warning(
                    "gmail sync raised",
                    extra={"app_user_id": source.user_id, "gmail_error": "unhandled"},
                )
                result = SyncOutcome.FAILED

            counts[result] = counts.get(result, 0) + 1
            self.stdout.write(f"  user={source.user_id} {getattr(result, 'value', result)}")

        summary = " ".join(f"{_label(k)}={v}" for k, v in counts.items() if v)
        self.stdout.write(f"gmail sync: {summary or 'nothing due'}")
        return None


def _label(key) -> str:
    return key.value if isinstance(key, SyncOutcome) else str(key)