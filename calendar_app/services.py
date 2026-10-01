"""Calendar queries. No HTTP, no templates — just the data the views need.

The view layer holds no permission logic and no queries (§7 rule 0). The CI grep
that fails the build on a query in views is what holds it.
"""

from __future__ import annotations

from datetime import date, timedelta

from calendar_app.models import CalendarEvent
from core.models import Source


def source_for(user, kind: str) -> Source | None:
    """The user's Source row of this kind, or None."""
    return Source.objects.filter(user=user, kind=kind).first()


def fortnight_events(owner, start: date, end: date):
    """Events for the visible fortnight, ordered by start_at."""
    return (
        CalendarEvent.objects.filter(owner=owner, start_at__date__gte=start, start_at__date__lte=end)
        .order_by("start_at")
        .select_related("owner")
    )


def today_and_fortnight() -> tuple[date, date]:
    """Today and today + 13 days = 14 days total."""
    from django.utils import timezone
    today = date.today()
    # Use Django's timezone to be consistent with the rest of the app
    return today, today + timedelta(days=13)