"""Gmail API payloads -> `message` rows, and the two rules that decide what a
thread looks like.

**Threads are a query, not a table.** §6.1's reasoning, compressed: a thread row
would hold a subject, a snippet and an unread count that are all derived from
its messages, so it needs denormalization and its own invalidation path for
fields Postgres can produce in one pass with `DISTINCT ON (thread_id)`. So
`feed_threads()` here returns one row per thread anchored on its newest message,
and the aggregation is done in Python *after* the page is bounded by a LIMIT —
never by N+1 queries.

**Unread is thread-wide, and that is a choice.** §6.2: a thread reads as unread
if *any* of its messages is unread. Gmail's own state is per-message, but a
person reads a thread top to bottom, so a partially-read thread reads as "read"
to a human. The one-line alternative is named in the spec; if the team prefers
per-message, `unread_count` is where it changes.

The unread *writer* rule is the other half of §6.2 and it lives in
`apply_unread_label` rather than in the upsert, because it is the only place
where two sources of truth meet: a changed Gmail label overwrites our local
flag, and an unchanged one writes nothing at all — which is what keeps idle
syncs cheap.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field

from django.utils import timezone

from mail.models import Message

# Gmail's UNREAD system label id. The spec resolves it once per sync via
# users.labels.get; caching it in-process for the run is what keeps this to at
# most two extra API calls per sync instead of two per message.
UNREAD_LABEL_NAME = "UNREAD"

# RFC 5322 addr-spec, deliberately loose. Gmail headers arrive in display form
# ("Ada Lovelace <ada@example.test>") and occasionally with folding whitespace,
# so this pulls the bracketed address out and leaves everything else alone.
_ADDRESS = re.compile(r"<([^>]+)>|([^\s<>,;]+@[^\s<>,;]+)")


def extract_addresses(raw) -> list[str]:
    """Addresses out of a Gmail `headers` entry, in order, de-duplicated.

    Gmail returns one comma-joined string, not a list, so this splits on commas
    before looking at each part — otherwise two recipients become one malformed
    address.
    """
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        candidates = []
        for item in raw:
            candidates.extend(str(item).split(","))
    else:
        candidates = str(raw).split(",")

    found: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        match = _ADDRESS.search(candidate)
        address = (match.group(1) or match.group(2)) if match else None
        if not address or "@" not in address:
            continue
        address = address.strip().strip("<>").lower()
        if address not in found:
            found.append(address)
    return found


def _header(headers, name: str) -> str | None:
    for entry in headers or ():
        if entry.get("name", "").lower() == name.lower():
            return entry.get("value")
    return None


def _body_text(payload: dict) -> str | None:
    """The plain part, if there is one.

    MIME is a tree and this walks one level of it, because Gmail's own default
    is a single `text/plain` alternative and a deep walk is unneeded weight.
    html-only has no plain part; §6.1 says the UI strips the HTML in that case,
    which is the view's business, not this function's.
    """
    body = payload.get("body") or {}
    data = body.get("data")
    if data and payload.get("mimeType", "").startswith("text/plain"):
        return _decode_base64url(data)
    for part in payload.get("parts") or ():
        if part.get("mimeType", "").startswith("text/plain") and part.get("body", {}).get("data"):
            return _decode_base64url(part["body"]["data"])
    return None


def _body_html(payload: dict) -> str | None:
    data = (payload.get("body") or {}).get("data")
    if data and payload.get("mimeType", "").startswith("text/html"):
        return _decode_base64url(data)
    for part in payload.get("parts") or ():
        if part.get("mimeType", "").startswith("text/html") and part.get("body", {}).get("data"):
            return _decode_base64url(part["body"]["data"])
    return None


def _decode_base64url(data: str) -> str:
    import base64

    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    """An HTML body reduced to readable text, for the no-plain-part case."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def readable_body(message: Message) -> str:
    """What the UI shows for one message.

    `body_text` when it exists, else the HTML stripped. §6.1 says the HTML is
    never *rendered*; reading it as text is not rendering it and is the
    difference between an empty message and a legible one.
    """
    if message.body_text:
        return message.body_text
    if message.body_html:
        return _strip_html(message.body_html)
    return message.snippet or ""


@dataclass
class NormalizedMessage:
    google_message_id: str
    thread_id: str
    subject: str
    snippet: str
    from_email: str
    from_name: str | None
    to_emails: list[str]
    cc_emails: list[str]
    received_at: datetime.datetime
    message_id_header: str | None
    in_reply_to: str | None
    body_text: str | None
    body_html: str | None
    has_attachment: bool
    is_read: bool
    label_ids: list[str] = field(default_factory=list)


def normalize_message(raw: dict, *, unread_label_id: str | None = None) -> NormalizedMessage:
    """One Gmail API message resource as the columns of `message`.

    `raw` is `users.messages.get` output: `payload` with `headers`, plus the
    `labelIds` from the same response. `unread_label_id` is the one resolved id
    from `users.labels.get`; passing it in rather than looking it up per message
    is what bounds the API cost at two calls per sync (§6.2).
    """
    payload = raw.get("payload") or {}
    headers = payload.get("headers") or []
    labels = raw.get("labelIds") or []

    internal_date = raw.get("internalDate")
    received_at = (
        datetime.datetime.fromtimestamp(int(internal_date) / 1000, tz=datetime.timezone.utc)
        if internal_date
        else timezone.now()
    )

    from_header = _header(headers, "From") or ""
    from_emails = extract_addresses(from_header)
    # display name is whatever precedes the angle bracket; absent for a bare
    # address, which is normal for machine senders
    display = re.sub(r"<[^>]*>", "", from_header).strip().strip('"') or None

    is_read = unread_label_id not in labels if unread_label_id else "UNREAD" not in labels

    return NormalizedMessage(
        google_message_id=raw["id"],
        thread_id=raw.get("threadId") or raw["id"],
        subject=_header(headers, "Subject") or "",
        snippet=raw.get("snippet") or "",
        from_email=from_emails[0] if from_emails else "",
        from_name=display,
        to_emails=extract_addresses(_header(headers, "To")),
        cc_emails=extract_addresses(_header(headers, "Cc")),
        received_at=received_at,
        message_id_header=_header(headers, "Message-ID"),
        in_reply_to=_header(headers, "In-Reply-To"),
        body_text=_body_text(payload),
        body_html=_body_html(payload),
        has_attachment=any(part.get("filename") for part in payload.get("parts") or ()),
        is_read=is_read,
        label_ids=labels,
    )


# ------------------------------------------------ §6.2: two writers, one rule


def apply_unread_label(message: Message, now_is_read: bool) -> bool:
    """Overwrite our local unread flag from Gmail. True when it wrote.

    No change means no write, and that is the point: an idle sync must not
    dirty a row nobody looked at. The decision is made against the row the
    database holds, not against a copy the caller carried in.
    """
    if message.is_read == now_is_read:
        return False
    Message.objects.filter(pk=message.pk).update(is_read=now_is_read)
    message.is_read = now_is_read
    return True


# ------------------------------------------------ §6.1: the feed


@dataclass
class FeedThread:
    thread_id: str
    subject: str
    snippet: str
    participants: list[str]
    last_activity: datetime.datetime
    unread_count: int
    is_priority: bool


def feed_threads(owner, *, limit: int = 50) -> list[FeedThread]:
    """The owner's threads, newest first, one row each.

    §6.1's shape is one query plus a Python fold, and the fold is *all* of the
    aggregation rather than just the awkward parts: the newest messages of the
    newest threads are fetched first, then every message of those threads is
    folded in the order it arrived. So participants (the union of `from_email`
    and `to_emails`), unread-any and priority-any all come out of rows already
    in hand — one statement, no N+1, and nothing that scales with thread depth.

    The sub-select picks which threads appear; the join brings in their messages.
    `MAX(received_at) DESC` in the sub-select is what orders the feed, and
    because the outer query re-sorts by the same column the first row of each
    group is its newest message — the anchor for subject and snippet.
    """
    rows = Message.objects.raw(
        """
        SELECT m.* FROM message m
        JOIN (
            SELECT thread_id, MAX(received_at) AS last_at
            FROM message
            WHERE owner_id = %s
            GROUP BY thread_id
            ORDER BY last_at DESC
            LIMIT %s
        ) newest ON newest.thread_id = m.thread_id
        WHERE m.owner_id = %s
        ORDER BY m.received_at DESC, m.id DESC
        """,
        [owner.pk, limit, owner.pk],
    )

    threads: dict[str, FeedThread] = {}
    for row in rows:
        thread = threads.get(row.thread_id)
        if thread is None:
            # first row for this thread: newest message, so it anchors the row
            thread = FeedThread(
                thread_id=row.thread_id,
                subject=row.subject,
                snippet=row.snippet,
                participants=[],
                last_activity=row.received_at,
                unread_count=0,
                is_priority=False,
            )
            threads[row.thread_id] = thread

        thread.unread_count += 0 if row.is_read else 1
        thread.is_priority = thread.is_priority or row.is_priority
        for address in (row.from_email, *row.to_emails):
            if address and address not in thread.participants:
                thread.participants.append(address)

    return list(threads.values())


def thread_messages(owner, thread_id: str) -> list[Message]:
    """One thread, oldest first. A single indexed read via idx_message_owner_thread."""
    return list(
        Message.objects.filter(owner=owner, thread_id=thread_id).order_by("received_at", "id")
    )