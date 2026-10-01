"""A materialized Google calendar event, one row per (owner, google_event_id).

The column set is §3.3's table and nothing was added for convenience. Two of the
choices are load-bearing rather than obvious:

* `google_event_id` is nullable and the unique index on (owner_id,
  google_event_id) is **partial** (`IS NOT NULL`). Seeded demo events have no
  Google id, and without the predicate every seeded row would collide with
  every other one for the same owner. The partial form also gives sync its
  idempotency for free: a retried sync after a partial failure upserts onto the
  same row instead of duplicating the window.
* `is_busy` is stored rather than derived. Google's `transparency` is the
  authoritative answer and re-deriving it from the title would be a guess; §5.2
  step 1 then filters on the stored flag.

`event_status` keeps `cancelled` as a row. Cancelling is an upsert, not a
delete, because "this meeting was called off" and "this meeting never existed"
are different facts and only the first one is true.
"""

from __future__ import annotations

from django.db import models

from accounts.models import AppUser

EVENT_STATUSES = ("confirmed", "tentative", "cancelled")
EVENT_SOURCES = ("google", "seed")


class CalendarEvent(models.Model):
    class EventStatus(models.TextChoices):
        CONFIRMED = "confirmed"
        TENTATIVE = "tentative"
        CANCELLED = "cancelled"

    class Source(models.TextChoices):
        GOOGLE = "google"
        SEED = "seed"

    owner = models.ForeignKey(AppUser, on_delete=models.CASCADE, related_name="events")
    # NULL for seeded rows; see the module docstring for why the index is partial.
    google_event_id = models.CharField(max_length=256, null=True, blank=True)
    calendar_id = models.CharField(max_length=256, null=True, blank=True)

    title = models.CharField(max_length=500, default="(no title)")
    description = models.TextField(null=True, blank=True)
    location = models.CharField(max_length=500, null=True, blank=True)

    start_at = models.DateTimeField()
    end_at = models.DateTimeField()

    is_all_day = models.BooleanField(default=False)
    is_busy = models.BooleanField(default=True)
    event_status = models.CharField(
        max_length=16, choices=EventStatus.choices, default=EventStatus.CONFIRMED
    )
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.GOOGLE)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(event_status__in=EVENT_STATUSES),
                name="calendar_event_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(source__in=EVENT_SOURCES),
                name="calendar_event_source_valid",
            ),
            # end_at > start_at is §3.3's check. A zero-length block would be a
            # slot of no duration in §5.2 and an overlap that can never resolve.
            models.CheckConstraint(
                condition=models.Q(end_at__gt=models.F("start_at")),
                name="calendar_event_end_after_start",
            ),
            models.UniqueConstraint(
                fields=["owner", "google_event_id"],
                name="calendar_event_owner_google_id_unique_nonnull",
                condition=models.Q(google_event_id__isnull=False),
            ),
        ]
        indexes = [
            # the highest-traffic index in the schema: every conflict query
            # filters by owner then scans a range
            models.Index(fields=["owner", "start_at"], name="cal_ev_owner_start_idx"),
            # cross-team overlap detection and the admin availability view, which
            # scan by time with no owner filter
            models.Index(fields=["start_at"], name="cal_ev_start_idx"),
            # availability always adds is_busy = true to an owner+range predicate,
            # which neither index above covers
            models.Index(
                fields=["owner", "is_busy", "start_at"], name="cal_ev_owner_busy_start_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.start_at.isoformat()} {self.title}"