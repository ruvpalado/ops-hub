"""One Gmail message, stored. Threads are a grouping over these rows, not a
table of their own — see §6.1 for why, in one paragraph, at length.

Three columns here are load-bearing and the comments say so:

* `idempotency_key` carries a unique index. It is the only place in this app
  where a duplicate row is an externally visible, hard-to-undo event (§9.4), so
  the guarantee is enforced by the database rather than by the view remembering
  to look. Null is allowed and means "this message was not sent by us", so the
  index is partial: requiring a key on inbound mail would be inventing 40,000
  meaningless values per mailbox.
* `body_html` is stored and never rendered in phase 1. It is an untrusted XSS
  vector from a third party, and the mitigation (a sandboxed iframe) is not
  half-built here; until someone builds it, the column is inert.
* `to_emails` / `cc_emails` are arrays, not a join table. The only question
  ever asked of them is "who is on this message", and the reply builder (§6.3)
  wants a first non-self entry — an array answers that without a second query.
"""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models

from accounts.models import AppUser
from core.models import Source

DIRECTIONS = ("inbound", "outbound")


class Message(models.Model):
    class Direction(models.TextChoices):
        INBOUND = "inbound"
        OUTBOUND = "outbound"

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="messages")
    owner = models.ForeignKey(AppUser, on_delete=models.CASCADE, related_name="messages")

    google_message_id = models.CharField(max_length=256)
    thread_id = models.CharField(max_length=256)
    # Google's own RFC 5322 Message-ID, needed to build In-Reply-To/References
    # in §6.3. Null because Gmail only returns it under a metadata scope we do
    # not always have.
    message_id_header = models.CharField(max_length=512, null=True, blank=True)
    in_reply_to = models.CharField(max_length=512, null=True, blank=True)

    subject = models.TextField(blank=True, default="")
    snippet = models.TextField(blank=True, default="")
    body_text = models.TextField(null=True, blank=True)
    # Stored, never rendered. See the module docstring.
    body_html = models.TextField(null=True, blank=True)

    from_email = models.CharField(max_length=320, blank=True, default="")
    from_name = models.CharField(max_length=320, null=True, blank=True)
    to_emails = ArrayField(models.CharField(max_length=320), default=list)
    cc_emails = ArrayField(models.CharField(max_length=320), default=list)

    direction = models.CharField(
        max_length=8, choices=Direction.choices, default=Direction.INBOUND
    )
    is_read = models.BooleanField(default=False)
    # A local flag, not a Gmail label: we hold no gmail.modify, so this is
    # ours to set and nothing upstream ever contradicts it.
    is_priority = models.BooleanField(default=False)
    received_at = models.DateTimeField()
    has_attachment = models.BooleanField(default=False)

    # Partial unique index — inbound rows have no key. See the docstring.
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        # The table is `message`, not Django's default `mail_message`. The spec
        # writes the feed query and the thread query against `message` in three
        # places (§3.5, §6.1) and those are hand-written SQL, so the name is
        # pinned rather than left to the app-label convention.
        db_table = "message"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(direction__in=DIRECTIONS), name="message_direction_valid"
            ),
            models.UniqueConstraint(
                fields=["owner", "google_message_id"],
                name="message_owner_google_id_unique",
            ),
            models.UniqueConstraint(
                fields=["idempotency_key"],
                name="message_idempotency_key_unique",
                condition=models.Q(idempotency_key__isnull=False),
            ),
        ]
        indexes = [
            # the inbox list's exact query: mine, unread first, newest first
            models.Index(fields=["owner", "is_read", "-received_at"], name="msg_owner_read_recv_idx"),
            # expanding a thread into its messages; without this, opening a
            # thread scans the user's whole mailbox
            models.Index(fields=["owner", "thread_id"], name="msg_owner_thread_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.thread_id}:{self.google_message_id}"