"""Gmail history walking, batching, and the one place the cursor is allowed to move.

§6.2's mechanics, and the reasons they are the way they are:

* **The cursor moves last.** `source.cursor` advances only after the upserts
  commit. A sync that dies halfway re-reads the same history next tick and lands
  on the same rows — the unique index on (owner_id, google_message_id) is what
  makes that repetition free. A cursor advanced first would skip messages nobody
  ever stored.

* **Bodies only for messages we do not have.** An already-stored message needs
  its metadata and labels (to notice an UNREAD flip) but not a second full body
  fetch. This is the difference between quota cost scaling with *changes* and
  quota cost scaling with *mailbox size*, and it is the mechanism behind
  Risk 7's ceiling.

* **Cold start is bounded.** No cursor means a 30-day seed capped at the most
  recent 200 threads. A team member with a decade of mail does not get a decade
  imported on first login.

* **429 is not an error state.** §9.4: `Retry-After` honoured verbatim,
  `next_attempt_at` deferred, no partial commit, `status` stays `active`. A rate
  limit is Google's budget, not a fault in this app. 410 is different — the
  cursor became invalid, so it is nulled and the next tick re-seeds.

The Google client is a parameter, not a global, exactly as in
`calendar_app/google_sync.py`: `service` is any object with
`history()`, `profile()`, `messages_list()`, `get_thread()`, and `get_message()`.
That is what lets the tests drive a 429, a 410, and a mid-sync failure at a
chosen call without an HTTP round-trip or a mocking library.
"""

from __future__ import annotations

import datetime
import enum
import logging

from django.db import transaction
from django.utils import timezone

from mail.models import Message
from mail.normalize import apply_unread_label, normalize_message

logger = logging.getLogger(__name__)

# One messages.get batch. Gmail's own guidance is 100 per request; going wider
# does not raise throughput, it only makes a partial failure lose more work.
BATCH_SIZE = 100

# §6.2's cold start, verbatim. Both bounds exist for one reason: a first sync
# must not be able to spend an hour or blow a daily quota on one backlog.
COLD_START_QUERY = "in:anywhere newer_than:30d"
COLD_START_THREAD_LIMIT = 200

# §9.4 sets no default, but "back off without an unbounded wait" needs one. 15
# minutes is the same figure §9.3 and §9.2 use for a transient upstream fault.
DEFAULT_RETRY_AFTER_SECONDS = 900


class SyncOutcome(enum.Enum):
    SYNCED = "synced"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    HISTORY_EXPIRED = "history_expired"


class RateLimited(Exception):
    """429. Carries Google's Retry-After in seconds, honoured verbatim."""

    def __init__(self, retry_after_seconds: int | None = None):
        super().__init__("rate limited")
        self.retry_after_seconds = retry_after_seconds


class HistoryExpired(Exception):
    """410. The historyId is no longer valid; the cursor must be cleared."""


def _batched(items: list[str], size: int = BATCH_SIZE):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _message_ids_from_history(response: dict) -> list[str]:
    """Ids touched by one history.list page, in the order Gmail reported them."""
    ids: list[str] = []
    for entry in response.get("history") or []:
        ids.extend(m["id"] for m in entry.get("messagesAdded") or [])
        # A label change arrives in messagesLabelsAdded, not messagesAdded.
        # Without this an UNREAD flip never reaches apply_unread_label and §6.2's
        # writer 1 silently stops working — a suite that only adds messages would
        # never notice, so the label-change test below is the only guard.
        for labelled in entry.get("messagesLabelsAdded") or []:
            ids.append(labelled["id"])
    return ids


def changed_message_ids(source, service) -> tuple[list[str], str | None]:
    """Walk history.list from the stored cursor.

    Returns `(ids, new_cursor)`. Raises `HistoryExpired` on 410 so the caller
    owns the cursor-clearing decision — it is the same decision as a successful
    sync's cursor write, and one place owning it is easier to reason about than
    two.
    """
    if not source.cursor:
        return _cold_start_ids(service), service.profile().get("historyId")

    ids: list[str] = []
    page_token = None
    while True:
        response = service.history(start_history_id=source.cursor, page_token=page_token)
        ids.extend(_message_ids_from_history(response))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    # de-duplicated, order preserved: the same id can appear in both lists
    return list(dict.fromkeys(ids)), service.profile().get("historyId")


def _cold_start_ids(service) -> list[str]:
    """Message ids for the bounded initial import.

    Bounded twice, per §6.2: 30 days, then the most recent 200 *threads*. The
    cap is on conversations, not messages, so one 500-message thread cannot eat
    the budget and starve the other 199.
    """
    listed = service.messages_list(query=COLD_START_QUERY, max_results=COLD_START_THREAD_LIMIT)
    ids: list[str] = []
    for thread in listed.get("threads", []):
        detail = service.get_thread(thread["id"])
        ids.extend(m["id"] for m in detail.get("messages", []))
    return ids


def fetch_and_upsert(owner, source, message_ids, *, service, unread_label_id=None):
    """Store the changed messages. Returns how many rows were written.

    One `metadata_only` flag is the whole quota story: a message already stored
    is re-read for labels and nothing else, so an idle mailbox costs one
    history.list page per tick no matter how much mail it holds.
    """
    stored = 0
    for batch in _batched(message_ids):
        already = set(
            Message.objects.filter(owner=owner, google_message_id__in=batch).values_list(
                "google_message_id", flat=True
            )
        )
        for message_id in batch:
            raw = service.get_message(message_id, metadata_only=message_id in already)
            parsed = normalize_message(raw, unread_label_id=unread_label_id)
            if message_id in already:
                # §6.2 writer 1: an unchanged label writes nothing at all, which is
                # the common case and the reason this is not an unconditional
                # update_or_create.
                row = Message.objects.get(owner=owner, google_message_id=message_id)
                if apply_unread_label(row, parsed.is_read):
                    stored += 1
                continue
            Message.objects.create(
                source=source,
                owner=owner,
                google_message_id=parsed.google_message_id,
                thread_id=parsed.thread_id,
                subject=parsed.subject,
                snippet=parsed.snippet,
                from_email=parsed.from_email,
                from_name=parsed.from_name,
                to_emails=parsed.to_emails,
                cc_emails=parsed.cc_emails,
                message_id_header=parsed.message_id_header,
                in_reply_to=parsed.in_reply_to,
                body_text=parsed.body_text,
                body_html=parsed.body_html,
                has_attachment=parsed.has_attachment,
                is_read=parsed.is_read,
                received_at=parsed.received_at,
            )
            stored += 1
    return stored


def _defer(source, now, retry_after_seconds: int | None) -> None:
    """A rate limit defers the work and changes nothing else.

    §9.4's row: `Retry-After` honoured verbatim, `status` stays `active`, no
    partial commits. Setting `status='error'` here would be the tempting mistake
    — it takes the user's mailbox out of the scheduler's work list for a
    condition that resolves on its own in seconds.
    """
    delay = retry_after_seconds or DEFAULT_RETRY_AFTER_SECONDS
    source.next_attempt_at = now + datetime.timedelta(seconds=delay)
    source.last_error_code = "rate_limited"
    source.status = source.Status.ACTIVE
    source.save(update_fields=["next_attempt_at", "last_error_code", "status", "updated_at"])


@transaction.atomic
def sync_gmail(source, service, *, now=None) -> SyncOutcome:
    """Sync one mailbox from its stored historyId."""
    now = now or timezone.now()

    try:
        ids, new_cursor = changed_message_ids(source, service)
    except RateLimited as exc:
        # Nothing has been written, so "no partial commits" needs no rollback —
        # the cursor and the rows are both untouched.
        _defer(source, now, exc.retry_after_seconds)
        logger.warning("gmail rate limited", extra={"app_user_id": source.user_id})
        return SyncOutcome.RATE_LIMITED
    except HistoryExpired:
        # §9.4: cursor nulled, 30-day re-seed on the next tick. Once. Nulling it
        # here and returning means the next tick takes the cold-start branch,
        # which is the behaviour the spec asks for.
        logger.warning("gmail history expired", extra={"app_user_id": source.user_id})
        source.cursor = None
        source.last_error_code = "sync_token_expired"
        source.save(update_fields=["cursor", "last_error_code", "updated_at"])
        return SyncOutcome.HISTORY_EXPIRED

    # §6.2's label refresh: the UNREAD label id is fetched once here and handed
    # to every message in this run, so a 250-message change costs the same two
    # calls as a one-message change.
    unread_label_id = service.labels_unread_id() if hasattr(service, "labels_unread_id") else None

    # The inner atomic is load-bearing, not decoration. A rate limit or a
    # network failure halfway through the batches must roll back the rows it had
    # already written ("no partial commits", §9.4) while leaving the outer
    # transaction healthy enough to commit the _defer() bookkeeping. Catching
    # inside one big transaction would roll the deferral back with it, and the
    # mailbox would be retried immediately with no backoff.
    try:
        with transaction.atomic():
            fetch_and_upsert(
                source.user, source, ids, service=service, unread_label_id=unread_label_id
            )
    except RateLimited as exc:
        _defer(source, now, exc.retry_after_seconds)
        logger.warning("gmail rate limited", extra={"app_user_id": source.user_id})
        return SyncOutcome.RATE_LIMITED
    except Exception:
        # Coarse code only, never the upstream body: §9.1 says a Google error
        # body can contain subject identifiers. The cursor is deliberately left
        # where it was, so the next tick re-reads this same history.
        logger.warning(
            "gmail sync failed", extra={"app_user_id": source.user_id, "gmail_error": "upsert_failed"}
        )
        return SyncOutcome.FAILED

    # Cursor last. Everything above has committed or this transaction has rolled
    # back, so the cursor can never point past a message nobody stored.
    source.cursor = new_cursor
    source.last_synced_at = now
    source.next_attempt_at = None
    source.last_error_code = None
    source.save(
        update_fields=["cursor", "last_synced_at", "next_attempt_at", "last_error_code", "updated_at"]
    )
    return SyncOutcome.SYNCED