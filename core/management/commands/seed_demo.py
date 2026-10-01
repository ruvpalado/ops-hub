"""A demo workspace: five people, a fortnight of work, and a mailbox that reads
like a real one.

This exists so a reviewer can click through every path the app has without
inventing data, and the seed is shaped by that job rather than by looking
plausible. Four decisions do the work:

**Seeded sources are `disconnected` with `last_error_code='no_token'`, not
`active`.** A source with no token is not connected. The scheduler skips it
exactly as it skips a real user whose grant expired, and the board renders behind
the same reconnect banner that a real stale mailbox renders behind — so the demo
exercises the *real* stale-but-visible path instead of a fabricated healthy one.
A seed marked `active` with no token would be a workspace that looks connected
and then fails on first tick, which teaches the reviewer the wrong thing.

**Tasks are shaped so all three permission outcomes are one click away.** Each of
the five users has their own tasks (row 8, editable), five are unassigned (row
8a, claimable), and eighteen are assigned across users (row 8, a 403 on edit). A
demo where every task belongs to the signed-in user proves nothing about rows 8
and 9.

**Threads carry a real Message-ID chain.** §6.3's reply builder threads a
conversation by In-Reply-To and References, and a seed whose ids are null would
make that path unexercisable without hand-editing rows. Each seeded message is
chained to its predecessor, and only the newest message in each thread is unread,
so the feed's "unread anywhere in the thread" rule and a partially-read thread are
both visible without editing anything.

**Idempotent, and refused outside development.** Every row is written through
`update_or_create` on a natural key the seed itself owns, so a second run changes
nothing and a seed interrupted half way resumes instead of doubling up. The
counts are asserted at import: a fixture whose shape drifts from the plan is a
failing import, not a quietly wrong demo.
"""

from __future__ import annotations

import datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import AppUser
from calendar_app.models import CalendarEvent
from core.models import Source
from mail.models import Message
from tasks.models import Task

# One domain, so `to_emails` has a plausible value and a message-id looks like a
# message-id. Not example.com: that one resolves, and a demo that can send mail
# at a domain somebody else owns is a demo with a bad habit in it.
DEMO_DOMAIN = "example.test"

PEOPLE = [
    # (email, display name, role) — one admin, four members, because row 9
    # (task deletion) is admin-only and there is no other way to demonstrate it.
    ("ada@example.test", "Ada Lovelace", AppUser.Role.ADMIN),
    ("grace@example.test", "Grace Hopper", AppUser.Role.MEMBER),
    ("alan@example.test", "Alan Turing", AppUser.Role.MEMBER),
    ("barbara@example.test", "Barbara Liskov", AppUser.Role.MEMBER),
    ("katherine@example.test", "Katherine Johnson", AppUser.Role.MEMBER),
]
OWNER = 0  # every seeded row belongs to PEOPLE[0]; the others are senders

# (title, assignee index or None, priority, status, days from today, hour due)
#
# Five unassigned on purpose (the claim path) and eighteen cross-assigned on
# purpose (the 403 path). Done rows carry `completed_at`, because a board where
# "done" has no completion date cannot answer "when did this land".
TASKS = [
    ("Rotate the on-call pager", 0, Task.Priority.HIGH, Task.Status.IN_PROGRESS, 0, 18),
    ("Review the Q3 supplier contract", 1, Task.Priority.NORMAL, Task.Status.TODO, 1, 12),
    ("Draft the incident postmortem template", 2, Task.Priority.NORMAL, Task.Status.TODO, 2, 17),
    ("Reconcile the September invoices", 3, Task.Priority.URGENT, Task.Status.TODO, 0, 9),
    ("Migrate the webhook retries", 4, Task.Priority.HIGH, Task.Status.IN_PROGRESS, 3, 14),
    ("Book the venue for the offsite", 0, Task.Priority.NORMAL, Task.Status.TODO, 5, 11),
    ("Audit the OAuth scopes we hold", 1, Task.Priority.HIGH, Task.Status.TODO, 1, 16),
    ("Fix the flaky calendar sync test", 2, Task.Priority.NORMAL, Task.Status.DONE, -2, 10),
    ("Write the runbook for a failed sync", 3, Task.Priority.NORMAL, Task.Status.TODO, 4, 15),
    ("Archive last quarter's tickets", None, Task.Priority.LOW, Task.Status.TODO, 6, 17),
    ("Replace the seed fixtures", None, Task.Priority.NORMAL, Task.Status.TODO, 7, 12),
    ("Interview for the platform role", None, Task.Priority.HIGH, Task.Status.TODO, 2, 10),
    ("Plan the December release", None, Task.Priority.NORMAL, Task.Status.TODO, 8, 14),
    ("Label the shared drives", None, Task.Priority.LOW, Task.Status.TODO, 9, 16),
    ("Cut the 2.1 release notes", 4, Task.Priority.NORMAL, Task.Status.DONE, -1, 13),
    ("Add a rate-limit dashboard", 0, Task.Priority.NORMAL, Task.Status.TODO, 5, 15),
    ("Retire the legacy webhook endpoint", 1, Task.Priority.URGENT, Task.Status.TODO, 0, 8),
    ("Refresh the onboarding docs", 2, Task.Priority.LOW, Task.Status.DONE, -3, 12),
    ("Answer the vendor security questionnaire", 3, Task.Priority.NORMAL, Task.Status.IN_PROGRESS, 2, 16),
    ("Cost out the storage migration", 4, Task.Priority.HIGH, Task.Status.TODO, 4, 13),
    ("Triage the inbox backlog", 0, Task.Priority.LOW, Task.Status.DONE, -1, 11),
    ("Fix the unread badge count", 1, Task.Priority.HIGH, Task.Status.DONE, -2, 14),
    ("Draft the Q4 hiring plan", 2, Task.Priority.NORMAL, Task.Status.TODO, 6, 15),
    ("Prune the unused feature flags", 3, Task.Priority.LOW, Task.Status.TODO, 8, 12),
    ("Reconcile the calendar with the board", 4, Task.Priority.NORMAL, Task.Status.TODO, 10, 16),
    ("Chase the signed vendor NDA", 0, Task.Priority.NORMAL, Task.Status.TODO, 3, 17),
    ("Clean up the staging database", 1, Task.Priority.NORMAL, Task.Status.TODO, 9, 15),
    ("Publish the on-call handbook", 2, Task.Priority.NORMAL, Task.Status.TODO, 11, 12),
]

# (day offset, hour, duration hours, title, all-day, busy)
#
# Fourteen days, busiest on days 0 and 1 so "today" has something in it and day
# 13 has one quiet event to show a boundary.
#
# All-day rows carry duration 0 and end at the *next* midnight, exclusive-end —
# which is what Google sends and what the `end_at > start_at` CHECK requires.
# `start == end` would be a zero-length slot, and §3.3 forbids it.
EVENTS = [
    (0, 9, 1, "Standup", False, True),
    (0, 11, 2, "Vendor call — storage", False, True),
    (0, 14, 1, "1:1 with Grace", False, True),
    (0, 16, 2, "Incident review", False, True),
    (1, 9, 1, "Standup", False, True),
    (1, 10, 3, "Deep work — webhooks", False, True),
    (1, 15, 1, "Recruiter screen", False, True),
    (2, 9, 1, "Standup", False, True),
    (2, 13, 2, "Design review", False, True),
    (2, 16, 1, "Retro", False, True),
    (2, 0, 0, "Payroll runs", True, False),
    (3, 9, 1, "Standup", False, True),
    (3, 11, 2, "Finance sync", False, True),
    (4, 9, 1, "Standup", False, True),
    (4, 14, 3, "Offsite planning", False, True),
    (5, 10, 4, "Quarterly planning", False, True),
    (5, 0, 0, "Company holiday", True, False),
    (6, 9, 1, "Standup", False, True),
    (6, 12, 1, "Lunch and learn", False, False),
    (7, 9, 1, "Standup", False, True),
    (7, 11, 2, "Security review", False, True),
    (8, 9, 1, "Standup", False, True),
    (8, 15, 2, "Roadmap", False, True),
    (9, 9, 1, "Standup", False, True),
    (9, 13, 3, "Customer advisory", False, True),
    (10, 9, 1, "Standup", False, True),
    (10, 11, 1, "Skip-level", False, True),
    (11, 9, 1, "Standup", False, True),
    (11, 14, 2, "Contract review", False, True),
    (12, 9, 1, "Standup", False, True),
    (12, 10, 2, "Onboarding session", False, True),
    (13, 9, 1, "Standup", False, True),
    (13, 16, 1, "Month-end wrap", False, True),
    (13, 0, 0, "Maintenance window", True, False),
    (0, 13, 1, "Focus block", False, False),
    (1, 17, 1, "Focus block", False, False),
    (2, 11, 1, "Focus block", False, False),
    (3, 17, 1, "Focus block", False, False),
    (4, 11, 1, "Focus block", False, False),
    (6, 17, 1, "Focus block", False, False),
    (7, 16, 1, "Focus block", False, False),
    (8, 11, 1, "Focus block", False, False),
    (9, 16, 1, "Focus block", False, False),
    (10, 17, 1, "Focus block", False, False),
    (11, 10, 1, "Focus block", False, False),
    (13, 10, 1, "Focus block", False, False),
]

# (subject, partner index in PEOPLE, [body per message])
#
# The partner sends the even-numbered message and the owner the odd one, so every
# thread has an inbound first message and the owner both sends and receives. §6.3
# replies to "the first non-self recipient", and a mailbox where the owner never
# sends would leave that rule untested.
#
# Lengths are deliberately uneven — one single-message thread, nine two-message
# threads, seven three — because the feed groups by thread and anchors on the
# newest message, and a seed where every thread has the same shape exercises
# neither.
THREADS = [
    ("Q4 budget review", 1, [
        "Can you send the draft before Thursday?",
        "Draft attached. Numbers moved since August.",
        "The storage line is the one that hurts — can we split it?",
    ]),
    ("Contract redlines", 2, [
        "Two clauses need your eyes.",
        "Line 14 is the expensive one.",
        "Agreed on 14. Leave 22 as-is.",
    ]),
    ("Invoice mismatch", 3, [
        "Line 4 does not match the purchase order.",
        "Confirmed — the PO was amended in August.",
    ]),
    ("Standup notes", 4, ["Posted in the doc."]),
    ("Offsite dates", 1, [
        "Three options in the poll.",
        "Option B works for me.",
        "B clashes on the 12th for me, so C.",
    ]),
    ("Security questionnaire", 2, [
        "Due Friday, 14 questions.",
        "Q9 asks about our retention policy — it is 30 days.",
        "That answers it. Sending it back.",
    ]),
    ("Flaky test", 3, [
        "The calendar sync test fails about one run in eight.",
        "Passes alone, fails in the suite — a clock boundary, not a race.",
        "Fixed by clamping to the day.",
    ]),
    ("Rate limit numbers", 4, [
        "We hit 429 three times last week.",
        "The header is honoured now, so it backs off instead of failing.",
        "Retries went from 40 to 4.",
    ]),
    ("Docs feedback", 1, [
        "The runbook assumes a clean install and does not survive a re-run.",
        "Rewritten to be resumable.",
    ]),
    ("On-call handover", 2, [
        "I am on until the 6th.",
        "Handing over Monday — thanks.",
    ]),
    ("OAuth scope audit", 3, [
        "Three scopes we no longer use.",
        "Dropping the Drive one needs a migration note first.",
    ]),
    ("Re: invoice mismatch", 4, [
        "Closed on our side.",
        "Thanks.",
    ]),
    ("Webhook retries", 1, [
        "Retries are double-sending on timeout.",
        "The idempotency key was missing.",
    ]),
    ("Vendor NDA", 2, [
        "Signed copy attached.",
        "Countersigned and filed.",
    ]),
    ("Accessibility pass", 3, [
        "Focus order is wrong on the board.",
        "Tab order fixed.",
        "Also fixed the contrast on the unread badge.",
    ]),
    ("Storage cost options", 4, [
        "Three tiers attached.",
        "The middle one balances out.",
    ]),
    ("Calendar sync timing", 1, [
        "Sync takes 40s for a fortnight.",
        "Mostly the per-id metadata fetches.",
    ]),
    ("Recruiter debrief", 2, [
        "Strong on systems, thin on product.",
        "Same read from the second interviewer.",
    ]),
    ("Unread badge", 3, [
        "Badge says 3, the list shows 5.",
        "Unread-any-per-thread was the bug.",
    ]),
    ("Offsite travel", 4, [
        "Flights are booked for the 9th.",
        "Return on the 13th, hotel sorted.",
    ]),
    ("Reply thread idempotency", 1, [
        "Double-clicking reply sent twice.",
        "The form key is now checked before send.",
        "Same key, second call is a no-op.",
    ]),
    ("Postmortem draft", 2, [
        "Incident 47 postmortem attached.",
        "Two actions are done; the third is on the board.",
    ]),
]

# The plan's counts, asserted rather than assumed. A fixture that drifts from
# its spec is a failing import, which is cheaper to notice than a demo board
# that is simply short two events for ever.
EXPECTED_USERS = 5
EXPECTED_TASKS = 28
EXPECTED_UNASSIGNED = 5
EXPECTED_EVENTS = 46
EXPECTED_THREADS = 22
EXPECTED_MESSAGES = 51
EXPECTED_DAYS = 14

assert len(PEOPLE) == EXPECTED_USERS, f"expected {EXPECTED_USERS} users"
assert len(TASKS) == EXPECTED_TASKS, f"expected {EXPECTED_TASKS} tasks"
assert sum(1 for t in TASKS if t[1] is None) == EXPECTED_UNASSIGNED, (
    f"expected {EXPECTED_UNASSIGNED} unassigned tasks"
)
assert len(EVENTS) == EXPECTED_EVENTS, f"expected {EXPECTED_EVENTS} events"
assert len(THREADS) == EXPECTED_THREADS, f"expected {EXPECTED_THREADS} threads"
assert sum(len(bodies) for _s, _p, bodies in THREADS) == EXPECTED_MESSAGES, (
    f"expected {EXPECTED_MESSAGES} messages"
)
assert max(day for day, *_ in EVENTS) - min(day for day, *_ in EVENTS) + 1 == EXPECTED_DAYS, (
    f"events must span {EXPECTED_DAYS} days"
)


class Command(BaseCommand):
    help = "Seed a demo workspace: 5 users, 28 tasks, 46 events, 22 threads / 51 messages."

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete this seed's rows first. Touches only rows the seed owns.",
        )

    def handle(self, *args, **options):
        # Refused outside development even with --flush: the guard is about the
        # command being run at all, because `seed_demo` with no arguments is a
        # plausible typo for something else and this one writes rows.
        if settings.OPSHUB_ENV != "development":
            raise CommandError(
                f"seed_demo refuses to run with OPSHUB_ENV={settings.OPSHUB_ENV!r}. "
                "It fabricates five accounts and a mailbox, which is a development "
                "fixture and not a migration."
            )

        now = timezone.now()
        if options["flush"]:
            self._flush()

        with transaction.atomic():
            users = self._users()
            self._sources(users, now)
            self._tasks(users, now)
            self._events(users, now)
            self._messages(users, now)

        self.stdout.write("Seed complete (idempotent)")
        self.stdout.write(
            f"  {len(PEOPLE)} users, {len(TASKS)} tasks, {len(EVENTS)} events, "
            f"{len(THREADS)} threads / {sum(len(b) for _s, _p, b in THREADS)} messages"
        )
        self.stdout.write(
            f"  every source is disconnected with no_token: sign in as {PEOPLE[OWNER][0]} "
            "to see the real reconnect banner"
        )

    # ------------------------------------------------ pieces

    def _flush(self) -> None:
        """Remove only what this seed made.

        By email, because the domain is the seed's own. A blanket truncate is
        shorter and destroys real work the first time this runs on a machine that
        has any — and `task.created_by` is PROTECT, so the users cannot be
        deleted until their tasks are, which is the constraint catching the
        mistake for us.
        """
        emails = [email for email, _name, _role in PEOPLE]
        counts = (
            Task.objects.filter(created_by__email__in=emails).delete(),
            Message.objects.filter(from_email__in=emails).delete(),
            Source.objects.filter(user__email__in=emails).delete(),
            AppUser.objects.filter(email__in=emails).delete(),
        )
        self.stdout.write(f"flushed {len(emails)} demo users and {counts[0][0]} of their rows")

    def _users(self) -> list[AppUser]:
        out = []
        for n, (email, name, role) in enumerate(PEOPLE):
            # google_sub is set so `require_member` treats these as real members
            # rather than unclaimed invitations: the demo has to reach the pages
            # behind the sign-in gate, not the gate itself.
            user, _created = AppUser.objects.update_or_create(
                email=email,
                defaults={
                    "display_name": name,
                    "role": role,
                    "google_sub": f"demo-sub-{n}",
                    "is_active": True,
                },
            )
            out.append(user)
        return out

    def _sources(self, users: list[AppUser], now) -> None:
        """Two sources per user, both disconnected with no_token.

        `no_token` is the spec's vocabulary and also the honest description: the
        row exists, the grant does not.

        `disconnected_at` is set, because that column is what the banner is keyed
        on: §4.5's failure mode is "a disconnected user whose feed silently stops
        updating", and the spec's answer is a banner for every user with
        `disconnected_at IS NOT NULL`. A seed that left it NULL would produce a
        workspace that looks connected and is not — the exact failure the column
        exists to make visible.
        """
        for user in users:
            for kind in (Source.Kind.GMAIL, Source.Kind.CALENDAR):
                Source.objects.update_or_create(
                    user=user,
                    kind=kind,
                    defaults={
                        "status": Source.Status.DISCONNECTED,
                        "last_error_code": "no_token",
                        "next_attempt_at": None,
                        "disconnected_at": now,
                    },
                )

    def _tasks(self, users: list[AppUser], now) -> None:
        for n, (title, assignee, priority, status, day, hour) in enumerate(TASKS):
            due = (now + datetime.timedelta(days=day)).replace(
                hour=hour, minute=0, second=0, microsecond=0
            )
            creator = users[assignee if assignee is not None else OWNER]
            # The natural key is the title, which this seed owns outright. A real
            # task called "Rotate the on-call pager" would collide; the fix then
            # is to rename the demo item, not to add a uuid column for it.
            Task.objects.update_or_create(
                title=title,
                defaults={
                    "assignee_id": users[assignee].pk if assignee is not None else None,
                    "created_by_id": creator.pk,
                    "priority": priority,
                    "status": status,
                    "due_at": due,
                    "description": f"Seeded demo task {n + 1} of {len(TASKS)}.",
                    "completed_at": due if status == Task.Status.DONE else None,
                },
            )

    def _events(self, users: list[AppUser], now) -> None:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for n, (day, hour, duration, title, all_day, busy) in enumerate(EVENTS):
            start = midnight + datetime.timedelta(days=day, hours=hour)
            owner = users[n % len(users)]
            CalendarEvent.objects.update_or_create(
                # source, owner, start_at and title together. Title alone will not
                # do it: there are fourteen "Standup" rows, and keying on
                # (owner, title) collapses them into five. google_event_id is null
                # for seeded rows by design (see the model's partial index), so
                # the start time is what makes a seeded event distinguishable.
                source=CalendarEvent.Source.SEED,
                owner=owner,
                start_at=start,
                title=title,
                defaults={
                    "start_at": start,
                    "end_at": start
                    + datetime.timedelta(days=1 if all_day else 0, hours=duration),
                    "is_all_day": all_day,
                    "is_busy": busy,
                    "event_status": CalendarEvent.EventStatus.CONFIRMED,
                    "last_synced_at": now,
                },
            )

    def _messages(self, users: list[AppUser], now) -> None:
        owner = users[OWNER]
        owner_source = owner.sources.get(kind=Source.Kind.GMAIL)
        partner_sources = {
            user.pk: user.sources.get(kind=Source.Kind.GMAIL)
            for user in users
            if user.pk != owner.pk
        }

        for t, (subject, partner_index, bodies) in enumerate(THREADS, start=1):
            thread_id = f"seed-thread-{t:02d}"
            partner = users[partner_index]
            partner_source = partner_sources[partner.pk]
            last = len(bodies) - 1
            # Spread the threads over the last three days so the feed has a real
            # date range rather than one timestamp repeated 51 times.
            thread_start = now - datetime.timedelta(days=2) + datetime.timedelta(
                minutes=t * 200
            )

            for n, body in enumerate(bodies):
                inbound = n % 2 == 0
                sender = partner if inbound else owner
                sender_source = partner_source if inbound else owner_source
                recipients = [owner.email if inbound else partner.email]

                Message.objects.update_or_create(
                    owner=owner,
                    google_message_id=f"seed-msg-{t:02d}-{n + 1}",
                    defaults={
                        "source_id": sender_source.pk,
                        "thread_id": thread_id,
                        # the chain §6.3 replies into, so the reply builder can be
                        # exercised without a real mailbox
                        "message_id_header": f"<seed-{t:02d}-{n + 1}@{DEMO_DOMAIN}>",
                        "in_reply_to": (
                            f"<seed-{t:02d}-{n}@{DEMO_DOMAIN}>" if n else None
                        ),
                        "subject": subject,
                        "snippet": body[:120],
                        "body_text": body,
                        # null on purpose: the seed must not hand the reviewer a
                        # body_html and quietly become the thing §6 forbids
                        "body_html": None,
                        "from_email": sender.email,
                        "from_name": sender.display_name,
                        "to_emails": recipients,
                        "cc_emails": [],
                        "direction": (
                            Message.Direction.INBOUND if inbound else Message.Direction.OUTBOUND
                        ),
                        # only the newest message is unread, so every thread is
                        # partially read and the unread-any rule is live
                        "is_read": n < last,
                        "is_priority": t in (3, 6, 21),
                        "received_at": thread_start + datetime.timedelta(minutes=n * 25),
                        "has_attachment": t == 6,
                    },
                )