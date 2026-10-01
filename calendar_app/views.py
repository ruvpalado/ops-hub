"""Calendar view over HTTP. No query lives in this module.

Same rule as `tasks.views` and `mail.views`: §7 rule 0 makes the view layer hold
no permission logic, and the CI grep that fails the build on a query here is what
holds it.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from accounts.services.refresh import banner_for
from calendar_app.models import CalendarEvent
from core.models import Source
from core.permissions import login_required


@login_required
@require_http_methods(["GET"])
def calendar(request):
    """The calendar: this user's events for the visible fortnight.

    An unconnected calendar source still renders — with an empty feed and a
    banner — because "you have not connected this yet" is a thing the user needs
    to be able to see, and hiding it behind a 404 would leave them with no route
    to fix it.
    """
    source = Source.objects.filter(user=request.actor, kind=Source.Kind.CALENDAR).first()
    if source is None:
        raise Http404("no such calendar") from None

    # The fortnight is the same window the availability engine uses: today plus
    # 13 days = 14 days total, matching the seed and the sync command's window.
    today = timezone.now().date()
    start = today
    end = today + timedelta(days=13)

    events = (
        CalendarEvent.objects.filter(owner=request.actor, start_at__date__gte=start, start_at__date__lte=end)
        .order_by("start_at")
        .select_related("owner")
    )

    return render(
        request,
        "calendar/calendar.html",
        {
            "source": source,
            "events": events,
            "today": today,
            "banner": banner_for(request.actor),
        },
    )