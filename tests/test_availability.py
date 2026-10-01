"""The availability algorithm, §5.2–§5.4, as pure functions.

Importable without ``django.setup()`` — the module-level import in every test
here is the proof. If someone adds a Django import to availability.py, the whole
file fails to collect, which is the intended alarm.

Every test name is prefixed with the part of the algorithm it covers, so
``-k "merge or overlap"`` selects the merge/overlap half on its own.
"""

from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

import pytest

from calendar_app.availability import (
    BusyBlock,
    Window,
    find_overlaps,
    merge_busy_blocks,
    next_free_slot,
)

ET = ZoneInfo("America/New_York")
DAY = timedelta(days=1)


def at(hour: int, minute: int = 0, *, day: int = 28, tz: tzinfo = UTC) -> datetime:
    """`at(24)` means midnight at the end of the day, not hour 24."""
    day += hour // 24
    return datetime(2026, 9, day, hour % 24, minute, tzinfo=tz)


def block(start: datetime, end: datetime, **kw) -> BusyBlock:
    return BusyBlock(start=start, end=end, **kw)


def spans(blocks) -> list[tuple[str, str]]:
    return [(b.start.isoformat(), b.end.isoformat()) for b in blocks]


# --------------------------------------------------------------------------
# §5.2 merge busy blocks
# --------------------------------------------------------------------------


def test_merge_empty_input_returns_empty():
    assert merge_busy_blocks([], Window(at(0), at(24))) == []


def test_merge_adjacent_blocks_becomes_one():
    """``end == next.start`` merges. A zero-length gap is not a free slot, so
    treating it as one would hand every downstream calculation a phantom gap."""
    merged = merge_busy_blocks(
        [block(at(9), at(10)), block(at(10), at(11))], Window(at(0), at(24))
    )
    assert spans(merged) == [(at(9).isoformat(), at(11).isoformat())]


def test_merge_three_deep_chain_becomes_one():
    merged = merge_busy_blocks(
        [block(at(9), at(10)), block(at(10), at(10, 30)), block(at(10, 30), at(11))],
        Window(at(0), at(24)),
    )
    assert len(merged) == 1
    assert merged[0].end == at(11)


def test_merge_nested_block_does_not_extend_the_outer_one():
    merged = merge_busy_blocks(
        [block(at(9), at(12)), block(at(10), at(10, 30))], Window(at(0), at(24))
    )
    assert spans(merged) == [(at(9).isoformat(), at(12).isoformat())]


def test_merge_keeps_a_genuine_gap_separate():
    merged = merge_busy_blocks(
        [block(at(9), at(10)), block(at(11), at(12))], Window(at(0), at(24))
    )
    assert len(merged) == 2


def test_merge_clips_to_the_window_edges():
    """An event running past the window contributes only the in-window part."""
    merged = merge_busy_blocks([block(at(8), at(20))], Window(at(9), at(17)))
    assert spans(merged) == [(at(9).isoformat(), at(17).isoformat())]


def test_merge_drops_transparent_cancelled_and_out_of_window():
    merged = merge_busy_blocks(
        [
            block(at(8), at(9), is_busy=False),
            block(at(10), at(11), event_status="cancelled"),
            block(at(6), at(7)),  # entirely before the window
            block(at(18), at(19)),  # entirely after
            block(at(12), at(13)),
        ],
        Window(at(9), at(17)),
    )
    assert spans(merged) == [(at(12).isoformat(), at(13).isoformat())]


def test_merge_is_order_independent():
    blocks = [block(at(11), at(12)), block(at(9), at(10)), block(at(10), at(11))]
    assert spans(merge_busy_blocks(blocks, Window(at(0), at(24)))) == spans(
        merge_busy_blocks(list(reversed(blocks)), Window(at(0), at(24)))
    )


# --------------------------------------------------------------------------
# §5.3 detect overlaps
# --------------------------------------------------------------------------


def test_overlap_flags_a_pair_over_the_threshold():
    found = find_overlaps([block(at(9), at(10), id=1), block(at(9, 30), at(11), id=2)])
    assert len(found) == 1
    assert (found[0].start, found[0].end) == (at(9, 30), at(10))
    assert (found[0].first_id, found[0].second_id) == (1, 2)


def test_overlap_ignores_a_pair_under_five_minutes():
    """Three minutes of double-booking is a back-to-back definition, not a
    conflict. Flagging it is how a detection feature loses its credibility."""
    found = find_overlaps([block(at(9), at(10), id=1), block(at(9, 57), at(11), id=2)])
    assert found == []


def test_overlap_flags_a_pair_at_exactly_five_minutes():
    found = find_overlaps([block(at(9), at(10), id=1), block(at(9, 55), at(11), id=2)])
    assert len(found) == 1
    assert found[0].minutes == 5


def test_overlap_never_pairs_an_event_with_itself():
    """The same event listed twice, or one event against itself, is not a clash."""
    found = find_overlaps([block(at(9), at(10), id=7)])
    assert found == []


def test_overlap_skips_transparent_and_cancelled():
    found = find_overlaps(
        [
            block(at(9), at(10), id=1),
            block(at(9, 30), at(11), id=2, is_busy=False),
            block(at(9, 30), at(11), id=3, event_status="cancelled"),
        ]
    )
    assert found == []


def test_overlap_ignores_disjoint_events():
    found = find_overlaps([block(at(9), at(10), id=1), block(at(11), at(12), id=2)])
    assert found == []


def test_overlap_reports_sorted_by_start():
    """Input order is deliberately reverse-chronological; output is not."""
    found = find_overlaps(
        [
            block(at(15), at(16), id=1),
            block(at(15, 30), at(16, 30), id=2),
            block(at(9), at(10), id=3),
            block(at(9, 30), at(10, 30), id=4),
        ]
    )
    assert [o.start for o in found] == [at(9, 30), at(15, 30)]


# --------------------------------------------------------------------------
# §5.4 next free slot
# --------------------------------------------------------------------------


def test_next_free_slot_worked_example_from_the_spec():
    """§5.4's worked example, verbatim: America/New_York, 60 minutes,
    Mon 2026-09-28 through Wed 2026-09-30.

    The spec lists four Monday candidates. Tuesday is entirely free, so the
    fifth candidate is Tuesday 09:00 ET — the cap is five and the days are
    walked in order. The spec's enumeration stopped at Monday; this test pins
    both halves so neither is an accident.
    """
    result = next_free_slot(
        owner_blocks=[
            block(at(13), at(14)),  # 09:00–10:00 ET
            block(at(15), at(16, 30)),  # 11:00–12:30 ET
            block(at(17), at(18)),  # 13:00–14:00 ET
        ],
        duration_minutes=60,
        window=Window(datetime(2026, 9, 28, 0, 0, tzinfo=ET), datetime(2026, 9, 30, 23, 59, tzinfo=ET)),
        timezone="America/New_York",
        now=datetime(2026, 9, 28, 8, 0, tzinfo=ET),
    )
    assert [s.start.astimezone(UTC).isoformat() for s in result.slots] == [
        "2026-09-28T14:00:00+00:00",  # the 10:00–11:00 ET gap, exactly 60 minutes
        "2026-09-28T18:00:00+00:00",
        "2026-09-28T19:00:00+00:00",
        "2026-09-28T20:00:00+00:00",
        "2026-09-29T13:00:00+00:00",  # Tuesday, wholly free
    ]


def test_next_free_slot_grid_is_aligned_to_the_local_clock_not_the_gap_start():
    """A day whose remaining window opens at 09:07 offers 09:15, not 09:07.
    Nobody wants a 9:07 meeting, and a grid-anchored slot does not move."""
    result = next_free_slot(
        owner_blocks=[],
        duration_minutes=30,
        window=Window(datetime(2026, 9, 28, 0, 0, tzinfo=ET), datetime(2026, 9, 28, 23, 59, tzinfo=ET)),
        timezone="America/New_York",
        now=datetime(2026, 9, 28, 9, 7, tzinfo=ET),
    )
    starts = [s.start.astimezone(ET).strftime("%H:%M") for s in result.slots]
    assert starts[0] == "09:15"


def test_next_free_slot_includes_free_time_before_the_first_conflict():
    """The cursor starts at the window start. An implementation that starts at
    the first busy block silently loses the whole morning."""
    result = next_free_slot(
        owner_blocks=[block(at(20), at(21))],
        duration_minutes=60,
        window=Window(datetime(2026, 9, 28, 0, 0, tzinfo=ET), datetime(2026, 9, 28, 23, 59, tzinfo=ET)),
        timezone="America/New_York",
        now=datetime(2026, 9, 28, 8, 0, tzinfo=ET),
    )
    assert [s.start.astimezone(ET).strftime("%H:%M") for s in result.slots] == [
        "09:00",
        "10:00",
        "11:00",
        "12:00",
        "13:00",
    ]


def test_next_free_slot_accepts_a_gap_exactly_equal_to_the_duration():
    """13:00–13:30 ET is 30 free minutes for a 30-minute meeting, wedged between
    a morning block and an afternoon one. No rounding, no tolerance: an exact
    fit is valid."""
    result = next_free_slot(
        owner_blocks=[block(at(9, tz=ET), at(13, tz=ET)), block(at(13, 30, tz=ET), at(17, tz=ET))],
        duration_minutes=30,
        window=Window(datetime(2026, 9, 28, 0, 0, tzinfo=ET), datetime(2026, 9, 28, 23, 59, tzinfo=ET)),
        timezone="America/New_York",
        now=datetime(2026, 9, 28, 8, 0, tzinfo=ET),
    )
    starts = [s.start.astimezone(ET).strftime("%H:%M") for s in result.slots]
    assert starts == ["13:00"]


def test_next_free_slot_rejects_a_gap_one_minute_short():
    """13:01–13:30 ET is 29 free minutes. Not a 30-minute meeting, so the whole
    day yields nothing and the caller gets a reason, not silence."""
    result = next_free_slot(
        owner_blocks=[block(at(9, tz=ET), at(13, 1, tz=ET)), block(at(13, 30, tz=ET), at(17, tz=ET))],
        duration_minutes=30,
        window=Window(datetime(2026, 9, 28, 0, 0, tzinfo=ET), datetime(2026, 9, 28, 23, 59, tzinfo=ET)),
        timezone="America/New_York",
        now=datetime(2026, 9, 28, 8, 0, tzinfo=ET),
    )
    starts = [s.start.astimezone(ET).strftime("%H:%M") for s in result.slots]
    assert starts == []
    assert result.reason is not None
    assert result.reason.code == "fully_booked"


def test_next_free_slot_caps_candidates_at_five():
    result = next_free_slot(
        owner_blocks=[],
        duration_minutes=30,
        window=Window(datetime(2026, 9, 28, 0, 0, tzinfo=ET), datetime(2026, 9, 28, 23, 59, tzinfo=ET)),
        timezone="America/New_York",
        now=datetime(2026, 9, 28, 8, 0, tzinfo=ET),
    )
    assert len(result.slots) == 5


def test_next_free_slot_skips_saturday_and_sunday():
    result = next_free_slot(
        owner_blocks=[],
        duration_minutes=60,
        window=Window(datetime(2026, 10, 3, 0, 0, tzinfo=ET), datetime(2026, 10, 4, 23, 59, tzinfo=ET)),
        timezone="America/New_York",
        now=datetime(2026, 10, 1, 0, 0, tzinfo=ET),
    )
    assert result.slots == []
    assert result.reason is not None
    assert result.reason.code == "outside_hours"


def test_next_free_slot_never_starts_in_the_past():
    """Nothing before `now` is offered, however free it looks on paper. The
    morning is entirely open and still must not be proposed."""
    result = next_free_slot(
        owner_blocks=[],
        duration_minutes=60,
        window=Window(datetime(2026, 9, 28, 0, 0, tzinfo=ET), datetime(2026, 9, 28, 23, 59, tzinfo=ET)),
        timezone="America/New_York",
        now=datetime(2026, 9, 28, 15, 0, tzinfo=ET),
    )
    assert result.slots
    assert all(s.start >= datetime(2026, 9, 28, 15, 0, tzinfo=ET) for s in result.slots)
    assert result.slots[0].start.astimezone(ET).strftime("%H:%M") == "15:00"


def test_next_free_slot_reports_a_reason_when_fully_booked():
    """Silence here is the failure mode. An empty list with a stated reason is
    a usable answer; an empty list alone is indistinguishable from a bug."""
    result = next_free_slot(
        owner_blocks=[block(at(9, tz=ET), at(17, tz=ET))],
        duration_minutes=60,
        window=Window(datetime(2026, 9, 28, 0, 0, tzinfo=ET), datetime(2026, 9, 28, 23, 59, tzinfo=ET)),
        timezone="America/New_York",
        now=datetime(2026, 9, 28, 8, 0, tzinfo=ET),
    )
    assert result.slots == []
    assert result.reason is not None
    assert result.reason.code == "fully_booked"
    assert not result


def test_next_free_slot_unions_busy_blocks_across_attendees():
    """09:00–11:00 ET and 10:00–12:00 ET overlap, so the union is one block
    09:00–12:00. An attendee free at 11:00 is not free if anyone else is
    busy, which is the whole point of asking several people at once."""
    result = next_free_slot(
        owner_blocks=[
            block(at(9, tz=ET), at(11, tz=ET)),
            block(at(10, tz=ET), at(12, tz=ET)),
        ],
        duration_minutes=60,
        window=Window(datetime(2026, 9, 28, 0, 0, tzinfo=ET), datetime(2026, 9, 28, 23, 59, tzinfo=ET)),
        timezone="America/New_York",
        now=datetime(2026, 9, 28, 8, 0, tzinfo=ET),
    )
    assert result.slots[0].start.astimezone(ET).strftime("%H:%M") == "12:00"


def test_next_free_slot_works_across_the_fall_back_transition():
    """Clocks go back an hour on 2026-11-01 in New York. A naive local-time
    implementation either repeats or skips an hour of availability."""
    result = next_free_slot(
        owner_blocks=[],
        duration_minutes=60,
        window=Window(datetime(2026, 11, 1, 0, 0, tzinfo=ET), datetime(2026, 11, 1, 23, 59, tzinfo=ET)),
        timezone="America/New_York",
        now=datetime(2026, 10, 31, 0, 0, tzinfo=ET),
    )
    starts = [s.start.astimezone(UTC).isoformat() for s in result.slots]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)
    assert all(datetime(2026, 11, 1, 14, tzinfo=UTC) <= datetime.fromisoformat(s) <= datetime(2026, 11, 1, 22, tzinfo=UTC) for s in starts)


def test_next_free_slot_works_across_the_spring_forward_transition():
    result = next_free_slot(
        owner_blocks=[],
        duration_minutes=60,
        window=Window(datetime(2026, 3, 9, 0, 0, tzinfo=ET), datetime(2026, 3, 9, 23, 59, tzinfo=ET)),
        timezone="America/New_York",
        now=datetime(2026, 3, 8, 0, 0, tzinfo=ET),
    )
    starts = [s.start.astimezone(UTC).isoformat() for s in result.slots]
    assert starts == sorted(starts)
    assert len(starts) == 5


def test_next_free_slot_rejects_a_duration_outside_the_enum():
    with pytest.raises(ValueError):
        next_free_slot(
            owner_blocks=[],
            duration_minutes=45.5,  # type: ignore[arg-type]
            window=Window(at(0), at(24)),
            timezone="UTC",
            now=at(0),
        )


def test_next_free_slot_rejects_a_window_over_thirty_one_days():
    with pytest.raises(ValueError):
        next_free_slot(
            owner_blocks=[],
            duration_minutes=60,
            window=Window(at(0), at(0) + timedelta(days=32)),
            timezone="UTC",
            now=at(0),
        )


def test_next_free_slot_reports_no_window_for_a_reversed_window():
    result = next_free_slot(
        owner_blocks=[],
        duration_minutes=60,
        window=Window(at(12), at(9)),
        timezone="UTC",
        now=at(0),
    )
    assert result.reason is not None
    assert result.reason.code == "no_window"