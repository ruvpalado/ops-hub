"""Materializing a Google calendar window into `calendar_event` rows.

The invariant this file exists to hold is §9.3's: **a failed sync never
deletes.** Everything else here — incremental sync tokens, the partial unique
index, cancellation — is bookkeeping in service of that one sentence.

So the tests are ordered by how much they are willing to break:

* a failure in the middle of a window leaves the previous window intact
  (test_a_failed_window_keeps_the_previous_window);
* a success that returns fewer events *does* remove the ones Google no longer
  lists, because a deleted event has to actually disappear;
* a cancelled event is upserted, not deleted, so its history survives;
* a retried sync after a partial failure cannot duplicate, which is what the
  partial unique index on (owner_id, google_event_id) is for.

The Google client is a stub with one method per API call and a scripted list of
failures, because a real HTTP round-trip would prove nothing about the code
that decides whether to delete.
"""

import datetime

import pytest

from accounts.models import AppUser
from calendar_app.google_sync import (
    SyncOutcome,
    delete_absent_events,
    sync_calendar,
    upsert_events,
)
from calendar_app.models import CalendarEvent
from core.models import Source

UTC = datetime.timezone.utc


def _at(hour, day=13, minute=0):
    return datetime.datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def _google_event(
    event_id,
    *,
    start=_at(9),
    end=_at(10),
    busy=True,
    status="confirmed",
    title="Standup",
    all_day=False,
):
    """One Google API event, shaped as the sync code consumes it."""
    start_field = {"date": start.date().isoformat()} if all_day else {
        "dateTime": start.isoformat().replace("+00:00", "Z"),
        "timeZone": "UTC",
    }
    end_field = {"date": end.date().isoformat()} if all_day else {
        "dateTime": end.isoformat().replace("+00:00", "Z"),
        "timeZone": "UTC",
    }
    return {
        "id": event_id,
        "summary": title,
        "description": "notes",
        "location": "Room 1",
        "status": status,
        "transparency": "opaque" if busy else "transparent",
        "start": start_field,
        "end": end_field,
    }


class FakeCalendarService:
    """The two calls the sync makes, and a script for what they return."""

    def __init__(self, events=(), *, page_token=None, new_token=None, fail_at=None):
        self._events = list(events)
        self._fail_at = fail_at
        self.page_token = page_token
        self.new_token = new_token
        self.calls = []

    def list_events(self, *, calendar_id, time_min, time_max, sync_token, page_token):
        self.calls.append(
            {"sync_token": sync_token, "page_token": page_token, "time_min": time_min}
        )
        if self._fail_at is not None and len(self.calls) == self._fail_at:
            raise RuntimeError("google 503")
        return {
            "items": self._events,
            "nextPageToken": None,
            "nextSyncToken": self.new_token,
        }


@pytest.fixture
def owner(db):
    return AppUser.objects.create(
        email="cal@example.com", display_name="Cal", google_sub="sub-cal"
    )


@pytest.fixture
def source(owner):
    return Source.objects.create(user=owner, kind=Source.Kind.CALENDAR)


def test_a_fresh_sync_writes_one_row_per_event(owner, source):
    service = FakeCalendarService(
        [_google_event("g1"), _google_event("g2", start=_at(11), end=_at(12))],
        new_token="sync-1",
    )

    outcome = sync_calendar(source, service, window_start=_at(0), window_end=_at(23))

    assert outcome is SyncOutcome.SYNCED
    rows = CalendarEvent.objects.filter(owner=owner).order_by("start_at")
    assert [r.google_event_id for r in rows] == ["g1", "g2"]
    assert all(r.source == CalendarEvent.Source.GOOGLE for r in rows)
    assert source.cursor == "sync-1"
    assert source.last_synced_at is not None


def test_a_second_sync_updates_rather_than_duplicates(owner, source):
    """The partial unique index is what makes this hold; without it, a retried
    sync after a partial failure duplicates every event in the window."""
    service = FakeCalendarService([_google_event("g1", title="Old title")], new_token="s1")
    sync_calendar(source, service, window_start=_at(0), window_end=_at(23))

    moved = FakeCalendarService(
        [_google_event("g1", title="New title", start=_at(14), end=_at(15))], new_token="s2"
    )
    sync_calendar(source, moved, window_start=_at(0), window_end=_at(23))

    row = CalendarEvent.objects.get(owner=owner, google_event_id="g1")
    assert CalendarEvent.objects.filter(owner=owner).count() == 1
    assert row.title == "New title"
    assert row.start_at == _at(14)


def test_a_failed_window_keeps_the_previous_window(owner, source):
    """§9.3: the difference between 'ten minutes stale' and 'this week is blank'."""
    good = FakeCalendarService([_google_event("g1")], new_token="s1")
    sync_calendar(source, good, window_start=_at(0), window_end=_at(23))

    broken = FakeCalendarService([_google_event("g2")], fail_at=1)
    outcome = sync_calendar(source, broken, window_start=_at(0), window_end=_at(23))

    assert outcome is SyncOutcome.FAILED
    assert list(CalendarEvent.objects.filter(owner=owner).values_list("google_event_id", flat=True)) == ["g1"]
    # the cursor does not move, so the next tick re-reads the same window
    assert source.cursor == "s1"


def test_a_successful_window_deletes_the_events_google_no_longer_lists(owner, source):
    """Deletion happens only after a *successful* full fetch of the window."""
    first = FakeCalendarService([_google_event("g1"), _google_event("g2")], new_token="s1")
    sync_calendar(source, first, window_start=_at(0), window_end=_at(23))

    second = FakeCalendarService([_google_event("g1")], new_token="s2")
    sync_calendar(source, second, window_start=_at(0), window_end=_at(23))

    assert list(CalendarEvent.objects.filter(owner=owner).values_list("google_event_id", flat=True)) == ["g1"]


def test_deletion_never_touches_a_different_owner(owner, source):
    mate = AppUser.objects.create(
        email="other@example.com", display_name="Other", google_sub="sub-other"
    )
    theirs = CalendarEvent.objects.create(
        owner=mate, google_event_id="g1", title="Theirs", start_at=_at(9), end_at=_at(10)
    )

    empty = FakeCalendarService([], new_token="s1")
    sync_calendar(source, empty, window_start=_at(0), window_end=_at(23))

    assert CalendarEvent.objects.filter(pk=theirs.pk).exists()


def test_a_cancelled_event_is_kept_and_marked_cancelled(owner, source):
    """Upsert, not delete: the row is what tells the board the meeting was
    called off, as opposed to never having existed."""
    first = FakeCalendarService([_google_event("g1")], new_token="s1")
    sync_calendar(source, first, window_start=_at(0), window_end=_at(23))

    cancelled = FakeCalendarService([_google_event("g1", status="cancelled")], new_token="s2")
    sync_calendar(source, cancelled, window_start=_at(0), window_end=_at(23))

    row = CalendarEvent.objects.get(owner=owner, google_event_id="g1")
    assert row.event_status == CalendarEvent.EventStatus.CANCELLED


def test_a_transparent_event_is_stored_but_not_busy(owner, source):
    """§5.2 step 1. The row is real; it just occupies no time."""
    service = FakeCalendarService([_google_event("g1", busy=False)], new_token="s1")

    sync_calendar(source, service, window_start=_at(0), window_end=_at(23))

    assert CalendarEvent.objects.get(google_event_id="g1").is_busy is False


def test_an_all_day_event_keeps_its_whole_day_span(owner, source):
    """An all-day event has `date`, not `dateTime`, on both ends. Naively
    parsing it as a datetime is the bug this test exists for."""
    service = FakeCalendarService(
        [_google_event("g1", start=_at(0), end=_at(23, day=14), all_day=True)], new_token="s1"
    )

    sync_calendar(source, service, window_start=_at(0), window_end=_at(23, day=14))

    row = CalendarEvent.objects.get(google_event_id="g1")
    assert row.is_all_day is True
    assert row.start_at < row.end_at


def test_the_sync_token_is_sent_when_we_have_one_and_omitted_when_we_do_not(owner, source):
    first = FakeCalendarService([_google_event("g1")], new_token="s1")
    sync_calendar(source, first, window_start=_at(0), window_end=_at(23))
    assert first.calls[0]["sync_token"] is None

    second = FakeCalendarService([], new_token="s2")
    sync_calendar(source, second, window_start=_at(0), window_end=_at(23))
    assert second.calls[0]["sync_token"] == "s1"


def test_a_window_with_no_next_sync_token_leaves_the_cursor_alone(owner, source):
    """Google omits nextSyncToken on an incremental page. Moving the cursor to
    None would silently force a full resync on every tick."""
    seeded = FakeCalendarService([_google_event("g1")], new_token="s1")
    sync_calendar(source, seeded, window_start=_at(0), window_end=_at(23))

    incremental = FakeCalendarService([], new_token=None)
    sync_calendar(source, incremental, window_start=_at(0), window_end=_at(23))

    assert source.cursor == "s1"


def test_an_event_moved_out_of_the_window_is_dropped_by_the_deletion_pass(owner, source):
    """A rescheduled meeting disappears from the old window without a delete
    ever being issued."""
    first = FakeCalendarService(
        [_google_event("g1", start=_at(9), end=_at(10))], new_token="s1"
    )
    sync_calendar(source, first, window_start=_at(8), window_end=_at(12))

    later = FakeCalendarService(
        [_google_event("g1", start=_at(20), end=_at(21))], new_token="s2"
    )
    sync_calendar(source, later, window_start=_at(20), window_end=_at(23))

    assert CalendarEvent.objects.filter(owner=owner, google_event_id="g1").count() == 1
    assert CalendarEvent.objects.get(google_event_id="g1").start_at == _at(20)


# ------------------------------------------------ the two units the sync is made of
#
# sync_calendar above is the composition; these are the pieces it calls, tested
# directly because that is where a regression would hide.

def test_upsert_events_returns_the_ids_google_gave_us(owner):
    written = upsert_events(owner, [_google_event("g1"), _google_event("g2")])
    assert written == ["g1", "g2"]


def test_delete_absent_events_removes_only_the_named_window(owner):
    inside = CalendarEvent.objects.create(
        owner=owner, google_event_id="in", title="In", start_at=_at(9), end_at=_at(10)
    )
    outside = CalendarEvent.objects.create(
        owner=owner, google_event_id="out", title="Out", start_at=_at(20), end_at=_at(21)
    )

    delete_absent_events(owner, keep=["g1"], window_start=_at(8), window_end=_at(12))

    assert CalendarEvent.objects.filter(pk=outside.pk).exists()
    assert not CalendarEvent.objects.filter(pk=inside.pk).exists()