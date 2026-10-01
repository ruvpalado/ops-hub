"""Calendar availability as pure functions.

No Django import, no I/O. This module must import without ``django.setup()``,
which is what makes the algorithm testable in milliseconds and lets the sync
layer and the view layer share one implementation instead of two.

Spec: §5.2 (merge busy blocks), §5.3 (detect overlaps), §5.4 (next free slot).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

BUSINESS_HOUR_START = 9
BUSINESS_HOUR_END = 17
OVERLAP_FLAG_MINUTES = 5
SLOT_GRID_MINUTES = 15
MAX_SLOT_CANDIDATES = 5
MAX_WINDOW_DAYS = 31
DURATIONS = (30, 45, 60, 90)


@dataclass(frozen=True)
class BusyBlock:
    """One event's occupancy of a calendar.

    ``id`` is only used by :func:`find_overlaps` to avoid pairing an event with
    itself. ``is_busy`` and ``event_status`` carry the two exclusions from §5.2:
    a transparent or cancelled event occupies no time.
    """

    start: datetime
    end: datetime
    id: int | None = None
    is_busy: bool = True
    event_status: str = "confirmed"


@dataclass
class Window:
    start: datetime
    end: datetime


@dataclass
class Overlap:
    first_id: int | None
    second_id: int | None
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass
class Slot:
    start: datetime
    end: datetime


@dataclass
class NoSlotReason:
    code: str
    detail: str = ""


@dataclass
class SlotResult:
    slots: list[Slot] = field(default_factory=list)
    reason: NoSlotReason | None = None

    def __bool__(self) -> bool:
        return bool(self.slots)


def merge_busy_blocks(blocks: Iterable[BusyBlock], window: Window) -> list[BusyBlock]:
    """Merge busy intervals inside ``window`` into disjoint blocks.

    Drops transparent and cancelled events, clips to the window, sorts, and
    merges. Adjacency is merged: ``end == next.start`` becomes one block,
    because a free slot needs strictly positive duration and a zero-length gap
    is not a slot. That is the difference between an off-by-one-duration bug in
    every downstream calculation and none.
    """
    clipped: list[BusyBlock] = []
    for block in blocks:
        if not block.is_busy or block.event_status == "cancelled":
            continue
        if block.end <= block.start:
            continue
        start = max(block.start, window.start)
        end = min(block.end, window.end)
        if end <= start:
            continue
        clipped.append(
            BusyBlock(start=start, end=end, id=block.id, is_busy=True, event_status=block.event_status)
        )

    clipped.sort(key=lambda b: (b.start, b.end))

    merged: list[BusyBlock] = []
    for block in clipped:
        if not merged or block.start > merged[-1].end:
            merged.append(block)
            continue
        last = merged[-1]
        if block.end > last.end:
            merged[-1] = BusyBlock(
                start=last.start, end=block.end, id=last.id, is_busy=True, event_status=last.event_status
            )
    return merged


def find_overlaps(blocks: Iterable[BusyBlock], threshold_minutes: float = OVERLAP_FLAG_MINUTES) -> list[Overlap]:
    """Overlaps within one person's events.

    Cross-owner double-booking is a different question and is not reported here.
    An overlap shorter than ``threshold_minutes`` is not returned: back-to-back
    definitions are not an operational problem, and flagging them makes the
    feature cry wolf.
    """
    live = [
        b
        for b in blocks
        if b.is_busy and b.event_status != "cancelled" and b.end > b.start
    ]
    threshold = timedelta(minutes=threshold_minutes)
    overlaps: list[Overlap] = []
    for i, a in enumerate(live):
        for b in live[i + 1 :]:
            if a.id is not None and a.id == b.id:
                continue
            if b.start >= a.end:
                continue
            start = max(a.start, b.start)
            end = min(a.end, b.end)
            if end - start >= threshold:
                overlaps.append(Overlap(first_id=a.id, second_id=b.id, start=start, end=end))
    overlaps.sort(key=lambda o: o.start)
    return overlaps


def _busy_days(tz: tzinfo) -> set[int]:
    """Monday-Friday. Deliberate simplification: no per-user business hours."""
    return {0, 1, 2, 3, 4}


def _business_windows(window: Window, tz: tzinfo, now: datetime) -> list[Window]:
    """Intersect ``window`` with business hours in ``tz``, day by day."""
    local_start = window.start.astimezone(tz)
    local_end = window.end.astimezone(tz)
    weekdays = _busy_days(tz)
    out: list[Window] = []

    day = local_start.date()
    last_day = local_end.date()
    while day <= last_day:
        if day.weekday() in weekdays:
            open_at = datetime(day.year, day.month, day.day, BUSINESS_HOUR_START, tzinfo=tz)
            close_at = datetime(day.year, day.month, day.day, BUSINESS_HOUR_END, tzinfo=tz)
            start = max(open_at, local_start, now.astimezone(tz))
            end = min(close_at, local_end)
            if end > start:
                out.append(Window(start=start, end=end))
        day += timedelta(days=1)
    return out


def _free_gaps(business: Window, busy: Sequence[BusyBlock]) -> list[tuple[datetime, datetime]]:
    """Subtract busy blocks from one business-hours window."""
    gaps: list[tuple[datetime, datetime]] = []
    cursor = business.start
    for block in busy:
        if block.end <= business.start:
            continue
        if block.start >= business.end:
            break
        if block.start > cursor:
            gaps.append((cursor, block.start))
        cursor = max(cursor, block.end)
    if cursor < business.end:
        gaps.append((cursor, business.end))
    return gaps


def _grid_ceiling(moment: datetime, tz: tzinfo) -> datetime:
    """Round up to the next 15-minute point on the local clock."""
    local = moment.astimezone(tz)
    minute = local.minute
    remainder = minute % SLOT_GRID_MINUTES
    if remainder == 0:
        return local
    bumped = local + timedelta(minutes=SLOT_GRID_MINUTES - remainder)
    return bumped.replace(second=0, microsecond=0)


def next_free_slot(
    owner_blocks: Iterable[BusyBlock],
    duration_minutes: int,
    window: Window,
    timezone: str,
    now: datetime,
    max_candidates: int = MAX_SLOT_CANDIDATES,
) -> SlotResult:
    """Up to ``max_candidates`` start times where every attendee is free.

    Returns a definite empty result with a reason when nothing fits. It never
    invents a slot, because a silent empty list is the failure mode that makes
    people stop trusting a scheduling tool.
    """
    if duration_minutes not in DURATIONS:
        raise ValueError(f"duration_minutes must be one of {DURATIONS}")
    # Five is a product decision about how much to show a person, not a knob the
    # caller gets to raise: more than five suggestions is noise, not help.
    max_candidates = min(max_candidates, MAX_SLOT_CANDIDATES)
    if (window.end - window.start) > timedelta(days=MAX_WINDOW_DAYS):
        raise ValueError(f"window may not exceed {MAX_WINDOW_DAYS} days")
    if window.end <= window.start:
        return SlotResult(reason=NoSlotReason("no_window", "window ends before it starts"))

    tz = ZoneInfo(timezone)
    duration = timedelta(minutes=duration_minutes)
    busy = merge_busy_blocks(owner_blocks, window)

    business = _business_windows(window, tz, now)
    if not business:
        return SlotResult(reason=NoSlotReason("outside_hours", "no business hours remain in the window"))

    conflicts_in_hours = False
    slots: list[Slot] = []
    for day_window in business:
        gaps = _free_gaps(day_window, busy)
        if not gaps:
            continue
        conflicts_in_hours = True
        for gap_start, gap_end in gaps:
            if gap_end - gap_start < duration:
                continue
            candidate = _grid_ceiling(gap_start, tz)
            while candidate + duration <= gap_end and len(slots) < max_candidates:
                slots.append(Slot(start=candidate.astimezone(UTC), end=(candidate + duration).astimezone(UTC)))
                # Step by whichever is longer, the grid interval or the
                # duration, so two proposals never overlap each other. The grid
                # is the alignment, not the stride: a 60-minute meeting in a
                # three-hour gap is offered at 14:00, 15:00, 16:00, not at
                # 14:00, 14:15, 14:30.
                candidate = candidate + max(duration, timedelta(minutes=SLOT_GRID_MINUTES))

    slots.sort(key=lambda s: s.start)
    if slots:
        return SlotResult(slots=slots)
    if not conflicts_in_hours:
        return SlotResult(reason=NoSlotReason("fully_booked", "every candidate day is busy for its whole span"))
    return SlotResult(reason=NoSlotReason("fully_booked", "no gap is long enough"))