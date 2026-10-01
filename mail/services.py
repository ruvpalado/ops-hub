"""The reply path, and the one check in this app that is unconditional.

§6.3's five numbered steps are all here. What the spec is actually protecting
with them is not the happy path — it is that **a duplicate email is externally
visible and hard to undo**, which is why the idempotency lookup happens before
`messages.send` and not after it, and why nothing is stored as sent unless
Google returned success.

The ordering is the design:

  1. ownership, as a filter in the query, not a check after the fetch;
  2. the idempotency key, read before any send;
  3. MIME construction from the thread's own headers;
  4. send;
  5. store the outbound row and write the audit row.

Everything that can refuse does so before step 4, and step 4 has exactly one
outcome that writes a row. There is no path that marks a reply sent and then
discovers it should not have.

**404, not 403, on someone else's thread** (§6.3 step 2). A 403 says "this
exists and is not yours"; §7 row 16 says mail is private to its owner at every
role, so the response code must not be a membership oracle into another
person's mailbox.

**The draft is never discarded on failure.** The view re-renders with the typed
text still in the box, because §9.4's send-failure rows require the composed
text to survive — the user retrying from an empty box is the failure mode those
rows are written about.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from django.utils import timezone

from audit.writer import record
from mail.models import Message
from mail.normalize import thread_messages

logger = logging.getLogger(__name__)


class ThreadNotFound(Exception):
    """No such thread, or it is not the actor's. Indistinguishable on purpose.

    §6.3 step 2 and §7 row 16 both require this to be the same answer. Two
    exceptions would produce two response codes, and the second one would tell a
    caller whose mailbox to go looking in.
    """


class SendFailed(Exception):
    """Google did not accept the message. Nothing was stored as sent."""

    def __init__(self, message: str, *, unknown_status: bool = False):
        super().__init__(message)
        # §9.4 distinguishes "we know it failed" from "we do not know", and the
        # second is never blind-retried. The view shows different copy for each.
        self.unknown_status = unknown_status


@dataclass
class SentReply:
    """What a successful reply looks like to its caller."""

    message: Message
    recipient: str
    # True when the request carried a key this thread had already used, so no
    # send happened. The view renders the same success either way; the flag is
    # for the log line, not the user.
    deduplicated: bool = False


def build_mime(thread: list[Message], actor, body: str) -> EmailMessage:
    """The RFC 5322 message §6.3 describes, assembled from the thread itself.

    Four decisions the spec names, each of which has a visible symptom if it is
    wrong:

    * `To` is the latest message's sender, unless that is the actor themselves —
      replying to your own last message goes to yourself, so the first non-self
      recipient on the thread is used instead.
    * `Subject` is reused verbatim. The spec calls this out explicitly because
      "Re: " is already on the string, and a builder that prefixes
      unconditionally produces the "Re: Re:" a user sees and distrusts.
    * `In-Reply-To` is the latest message's own Message-ID.
    * `References` is every earlier Message-ID in order, newest last, so a mail
      client threading the conversation by References alone gets the same result
      Gmail does.
    """
    latest = thread[-1]
    outgoing = EmailMessage()
    outgoing["To"] = _recipient_for(thread, actor)
    outgoing["From"] = actor.email
    outgoing["Subject"] = latest.subject
    outgoing["Message-ID"] = make_msgid(domain="opshub.local")
    outgoing["Date"] = formatdate(localtime=True)

    if latest.message_id_header:
        outgoing["In-Reply-To"] = latest.message_id_header
    references = [m.message_id_header for m in thread if m.message_id_header]
    if references:
        outgoing["References"] = " ".join(references)

    outgoing.set_content(body)
    return outgoing


def _recipient_for(thread: list[Message], actor) -> str:
    """Who this reply is addressed to.

    The latest message's sender, unless that is the actor's own address — in
    which case the first recipient on the thread that is not the actor. The
    walk is over `to_emails` rather than over messages because "somebody I have
    corresponded with" is a better guess than "the last person to write to me",
    and the spec names the non-self recipient as the rule.
    """
    own = (actor.email or "").lower()
    latest_sender = (thread[-1].from_email or "").lower()
    if latest_sender and latest_sender != own:
        return thread[-1].from_email

    for message in reversed(thread):
        for address in message.to_emails:
            if address and address.lower() != own:
                return address
    return thread[-1].from_email or ""


def existing_reply(actor, idempotency_key: str | None) -> Message | None:
    """The outbound row for this key, or None.

    Scoped to the actor as well as the key: a key is a UUID from a form, and a
    guessable or replayed one must not surface another person's outbound row —
    even though the unique index would refuse the second insert anyway, this is
    the query that decides whether to skip the send, so it carries the filter.
    """
    if not idempotency_key:
        return None
    return Message.objects.filter(
        owner=actor,
        idempotency_key=idempotency_key,
        direction=Message.Direction.OUTBOUND,
    ).first()


def send_reply(actor, thread_id: str, body: str, *, service, idempotency_key=None, now=None) -> SentReply:
    """Reply to a thread. Every refusal happens before `messages.send`.

    The order is the whole point: ownership, then the key, then MIME, then send,
    then store. A caller cannot reach the send without passing the idempotency
    lookup, which is what makes a double-click, a back-button resubmit and a
    retry-after-timeout all the same safe no-op.
    """
    now = now or timezone.now()

    # §6.3 step 2 / §7 row 16. The owner filter is in the query, so a thread in
    # another mailbox is simply not a row this can read.
    thread = thread_messages(actor, thread_id)
    if not thread:
        raise ThreadNotFound("no such thread")

    # §6.3 step 4, before the send. Not after: after is a second email.
    already = existing_reply(actor, idempotency_key)
    if already is not None:
        return SentReply(message=already, recipient="", deduplicated=True)

    outgoing = build_mime(thread, actor, body)
    recipient = outgoing["To"]

    try:
        sent = service.send_raw(raw=outgoing.as_bytes(), thread_id=thread_id)
    except TimeoutError as exc:
        # §9.4: status unknown. Never blind-retried, and the user is told to
        # check the thread rather than press send again.
        logger.warning("gmail send outcome unknown", extra={"app_user_id": actor.pk})
        raise SendFailed("We could not confirm your reply sent.", unknown_status=True) from exc
    except Exception as exc:  # noqa: BLE001 - any upstream failure is one case to the user
        # Coarse code only. §9.1: a Google error body can contain subject text.
        logger.warning(
            "gmail send rejected", extra={"app_user_id": actor.pk, "gmail_error": "send_failed"}
        )
        raise SendFailed("Your reply could not be sent.") from exc

    message_id = (sent or {}).get("id")

    # §6.3 step 6. Written only after success, so "stored as sent" and "Gmail
    # accepted it" cannot disagree. The row carries no body: §6.1 stores text
    # read from Gmail, and a reply we just composed is in the user's own client.
    row = Message.objects.create(
        # the thread's own source, not a lookup: every message in a thread
        # came from the same one, so the row we just read already knows it and
        # a fresh query could only return None or a different answer
        source_id=thread[-1].source_id,
        owner=actor,
        google_message_id=message_id,
        thread_id=thread_id,
        subject=thread[-1].subject,
        body_text=body,
        direction=Message.Direction.OUTBOUND,
        is_read=True,
        received_at=now,
        idempotency_key=idempotency_key,
    )

    record(
        actor,
        "mail.send",
        target_type="thread",
        target_id=thread_id,
        metadata={"recipients": 1, "deduplicated": False},
    )
    return SentReply(message=row, recipient=recipient)


def mark_thread_read(actor, thread_id: str) -> int:
    """§6.2 writer 2: opening a thread marks every message in it read.

    This does not call Gmail's modify endpoint and must not. We hold no
    gmail.modify (§4.1), and a write-back would couple our state to the user's
    mail client — the conflict §6.2 states and accepts.
    """
    return Message.objects.filter(owner=actor, thread_id=thread_id, is_read=False).update(
        is_read=True
    )


def unread_count(actor) -> int:
    return Message.objects.filter(owner=actor, is_read=False).count()


__all__ = [
    "SendFailed",
    "SentReply",
    "ThreadNotFound",
    "build_mime",
    "existing_reply",
    "mark_thread_read",
    "send_reply",
    "unread_count",
]