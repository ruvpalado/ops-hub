"""Calendar view over HTTP. No query lives in this module.

Same rule as `tasks.views` and `mail.views`: §7 rule 0 makes the view layer hold
no permission logic, and the CI grep that fails the build on a query here is what
holds it.
"""

from __future__ import annotations

from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from accounts.services.refresh import banner_for
from calendar_app.services import fortnight_events, source_for, today_and_fortnight
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
    source = source_for(request.actor, Source.Kind.CALENDAR)
    if source is None:
        raise Http404("no such calendar") from None

    today, end = today_and_fortnight()
    events = fortnight_events(request.actor, today, end)

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