"""The scheduler's work list: rows that are worth spending an API call on.

status='active' lives in the WHERE clause, not in Python. A disconnected or
needs-reauth account must cost zero API calls, and the only way to guarantee
that is for the database to never hand the row over — a status check done
after fetching has already spent the call.
"""

from __future__ import annotations

from django.db.models import Q
from django.utils import timezone

from core.models import Source


def due_sources(*, now=None) -> Source.objects:
    """Active sources that are due: never attempted, or due for a retry."""
    now = now or timezone.now()
    return Source.objects.filter(status=Source.Status.ACTIVE).filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
    )