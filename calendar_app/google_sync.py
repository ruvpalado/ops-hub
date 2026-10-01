"""Google Calendar window -> `calendar_event` rows.

The single rule everything else serves is §9.3's: **a failed sync never
deletes.** So the order of operations is not a style choice:

  1. upsert every event in the window (repeats are free — the partial unique
     index on (owner_id, google_event_id) makes a retry land on the same row);
  2. delete the rows the successful full fetch did *not* mention, and only
     inside the fetched window;
  3. move the cursor, and only if Google handed back a nextSyncToken.

Steps 1 and 2 share one transaction. A network failure during step 1 aborts
before step 2 runs at all, which is why a partial failure leaves the previous
window intact rather than half-replaced — "ten minutes stale" instead of
"this week is blank".

Two Google quirks are handled here rather than pushed onto callers:

* an all-day event carries `date` on `start`/`end`, not `dateTime`, and its end
  is exclusive. Parsing both with `fromisoformat` on the wrong key raises;
  silently producing a zero-length block would be worse.
* an incremental page often omits `nextSyncToken`. Setting the cursor to None
  would force a full resync on every tick, so a missing token leaves it alone.

The Google client is a parameter, not a global. `sync_calendar` takes a service
object with one `list_events(...)` method, which is what lets the tests drive
a failure at a chosen call without an HTTP round-trip or a mocking library.
"""

from __future__ import annotations

import datetime
import enum
import logging

from django.db import transaction
from django.utils import timezone

from calendar_app.models import CalendarEvent

logger = logging.getLogger(__name__)


class SyncOutcome(enum.Enum):
    SYNCED = "synced"
    FAILED = "failed"


def _parse_temporal(field: dict) -> datetime.datetime:
    """One side of an event, whichever shape Google used.

    All-day events carry `date` on both ends and the end is **exclusive** — a
    one-day event on the 13th has `end.date == 14`. So both sides anchor at
    midnight and the end is used as-is: pushing it forward a day here would
    double-count, and clipping to 23:59:59 would make `end_at > start_at`
    true for a zero-day span that the CHECK constraint is there to reject.
    """
    if "dateTime" in field:
        parsed = datetime.datetime.fromisoformat(field["dateTime"].replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed
    return datetime.datetime.combine(
        datetime.date.fromisoformat(field["date"]),
        datetime.time(0, 0),
        tzinfo=datetime.timezone.utc,
    )


def _normalize(raw: dict) -> dict:
    """A Google API event as the columns of `calendar_event`."""
    return {
        "google_event_id": raw["id"],
        "title": raw.get("summary") or "(no title)",
        "description": raw.get("description") or None,
        "location": raw.get("location") or None,
        "start_at": _parse_temporal(raw["start"]),
        "end_at": _parse_temporal(raw["end"]),
        "is_all_day": "date" in raw.get("start", {}),
        "is_busy": raw.get("transparency", "opaque") != "transparent",
        "event_status": raw.get("status", CalendarEvent.EventStatus.CONFIRMED),
        "source": CalendarEvent.Source.GOOGLE,
    }


def upsert_events(owner, events, *, now=None) -> list[str]:
    """Write or update each event. Returns the Google ids that are now stored."""
    now = now or timezone.now()
    written: list[str] = []
    for raw in events:
        fields = _normalize(raw)
        event_id = fields["google_event_id"]
        CalendarEvent.objects.update_or_create(
            owner=owner,
            google_event_id=event_id,
            defaults={**fields, "last_synced_at": now},
        )
        written.append(event_id)
    return written


def delete_absent_events(owner, keep, *, window_start, window_end) -> int:
    """Remove the owner's Google rows inside the window that were not returned.

    The window bound is what stops a *windowed* sync from eating the owner's
    whole history: only rows the fetch was supposed to cover are candidates.
    Seeded rows are excluded by `source` for the same reason §3.3's unique index
    is partial — they are not Google's to delete.
    """
    return CalendarEvent.objects.filter(
        owner=owner,
        source=CalendarEvent.Source.GOOGLE,
        start_at__gte=window_start,
        start_at__lt=window_end,
    ).exclude(google_event_id__in=keep).delete()[0]


@transaction.atomic
def sync_calendar(source, service, *, window_start, window_end, now=None) -> SyncOutcome:
    """Sync one calendar source over one window.

    `service` is any object with
    `list_events(*, calendar_id, time_min, time_max, sync_token, page_token)`,
    which keeps the Google client out of this module's imports and out of its
    tests.
    """
    now = now or timezone.now()
    calendar_id = getattr(source, "external_id", None) or "primary"

    try:
        response = service.list_events(
            calendar_id=calendar_id,
            time_min=window_start,
            time_max=window_end,
            sync_token=source.cursor,
            page_token=None,
        )
    except Exception:
        # Logged as a coarse code, never the upstream body: §9.1 says Google
        # error bodies can contain subject identifiers.
        logger.warning(
            "calendar sync failed",
            extra={"app_user_id": source.user_id, "calendar_error": "list_failed"},
        )
        return SyncOutcome.FAILED

    events = response.get("items") or []
    written = upsert_events(source.user, events, now=now)
    delete_absent_events(source.user, written, window_start=window_start, window_end=window_end)

    # Written through the instance rather than a queryset .update() so the
    # caller's object is not left stale — the scheduler reads source.cursor
    # immediately after this returns, and a stale in-memory copy would send the
    # next tick an empty sync token.
    source.last_synced_at = now
    source.next_attempt_at = None
    source.last_error_code = None
    next_token = response.get("nextSyncToken")
    if next_token:
        source.cursor = next_token
    source.save(
        update_fields=["last_synced_at", "next_attempt_at", "last_error_code", "cursor", "updated_at"]
    )

    return SyncOutcome.SYNCED