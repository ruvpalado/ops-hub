"""Which calendar sources this run would sync.

At step 13 this command is deliberately the *selection* half only: it resolves
the work list and reports it. Fetching and applying events lands with the
calendar step; what matters now, and what the step-13 test asserts against the
database, is that a non-active source is never selected — a disconnected user
must cost zero API calls.
"""

from django.core.management.base import BaseCommand

from core.scheduler import due_sources


class Command(BaseCommand):
    help = "Report the calendar sources eligible for a sync run."

    def add_arguments(self, parser):
        parser.add_argument(
            "--kind", default="calendar", help="Source kind to consider (default: calendar)."
        )

    def handle(self, *args, **options):
        rows = due_sources().filter(kind=options["kind"]).select_related("user")
        self.stdout.write(f"due {options['kind']} sources: {rows.count()}")
        for source in rows:
            self.stdout.write(f"  user={source.user_id} kind={source.kind} cursor={source.cursor or '-'}")
        return None
