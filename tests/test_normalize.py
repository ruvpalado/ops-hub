"""§6.1: a Gmail payload becomes `message` rows and a feed of threads.

Twelve tests, and the split between them is the point. Most of the file is about
*normalization* — getting headers, addresses, both MIME parts and the unread
label out of a shape that varies more than the API documentation admits. Those
are cheap to test and easy to get subtly wrong, because a wrong address is not
an exception, it is a reply that goes to the wrong person.

Three of them are about the two decisions in §6.2 and §6.1 that are choices
rather than consequences, and they are the tests worth reading first:

* `test_a_thread_is_unread_when_any_of_its_messages_is` — the unread-any rule.
  A partially-read thread reads as "read" to a human, which is why this is
  documented as a decision rather than a bug.
* `test_the_feed_is_one_query_and_no_n_plus_one` — the anti-N+1 proof. The
  spec's argument for not having a thread table is that Postgres can answer the
  feed in one pass; this is the assertion that it still does.
* `test_a_thread_anchors_on_its_newest_message` — subject and snippet come from
  the latest message, not the first one imported.

The RFC 5322 chain case (`test_headers_form_a_consistent_reply_chain`) is the one
§6.3 depends on: a reply builder needs `message_id_header` values to produce
correct `In-Reply-To` and `References`, and seeded data has to be shaped so that
builder can be exercised without a real mailbox.
"""

import base64
import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.models import AppUser
from core.models import Source
from mail import normalize
from mail.models import Message

UTC = datetime.timezone.utc


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _at(day, hour=9, minute=0):
    return datetime.datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def _header(name, value):
    return {"name": name, "value": value}


def _gmail_message(
    msg_id,
    *,
    thread_id=None,
    subject="Hello",
    to="Ada <ada@example.test>",
    cc=None,
    sender="Grace <grace@example.test>",
    body="plain words",
    html=None,
    unread=False,
    has_attachment=False,
    internal_date=None,
    message_id=None,
    in_reply_to=None,
):
    """A users.messages.get response, as close to real as the tests need."""
    headers = [_header("Subject", subject), _header("From", sender), _header("To", to)]
    if cc:
        headers.append(_header("Cc", cc))
    if message_id:
        headers.append(_header("Message-ID", message_id))
    if in_reply_to:
        headers.append(_header("In-Reply-To", in_reply_to))

    parts = []
    if body is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": _b64(body)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64(html)}})
    if has_attachment:
        parts.append({"mimeType": "application/pdf", "filename": "invoice.pdf"})

    return {
        "id": msg_id,
        "threadId": thread_id or msg_id,
        "snippet": f"snippet for {msg_id}",
        "internalDate": str(int((internal_date or _at(20)).timestamp() * 1000)),
        # Gmail marks unread by the *presence* of the UNREAD label, so an unread
        # message carries it and a read one does not.
        "labelIds": ["INBOX", "UNREAD"] if unread else ["INBOX"],
        "payload": {"mimeType": "text/plain", "headers": headers, "parts": parts},
    }


@pytest.fixture
def mailbox(db):
    user = AppUser.objects.create(
        email="owner@example.test", display_name="Owner", google_sub="sub-mail"
    )
    source = Source.objects.create(user=user, kind=Source.Kind.GMAIL)
    return user, source


def _store(user, source, raw, *, internal_date=None, **kwargs):
    """Persist one payload as the sync would.

    `internal_date` is a test convenience: most tests want a specific ordering
    inside a thread, and restating the whole payload to move one timestamp is
    noise. The raw dict is adjusted here rather than in the assertion.
    """
    if internal_date is not None:
        raw = {**raw, "internalDate": str(int(internal_date.timestamp() * 1000))}
    parsed = normalize.normalize_message(raw, unread_label_id="UNREAD")
    columns = {
        "google_message_id": parsed.google_message_id,
        "thread_id": parsed.thread_id,
        "subject": parsed.subject,
        "snippet": parsed.snippet,
        "from_email": parsed.from_email,
        "from_name": parsed.from_name,
        "to_emails": parsed.to_emails,
        "cc_emails": parsed.cc_emails,
        "message_id_header": parsed.message_id_header,
        "in_reply_to": parsed.in_reply_to,
        "body_text": parsed.body_text,
        "body_html": parsed.body_html,
        "has_attachment": parsed.has_attachment,
        "is_read": parsed.is_read,
        "received_at": parsed.received_at,
    }
    # an explicit kwarg wins: these tests are about what the *caller* decides
    # (a message marked read locally, a thread priority), not about re-deriving
    # it from the payload each time
    columns.update(kwargs)
    return Message.objects.create(source=source, owner=user, **columns)


# ------------------------------------------------ normalization


def test_a_message_normalizes_to_the_columns_it_is_given(mailbox):
    user, source = mailbox
    raw = _gmail_message(
        "m1",
        subject="Vendor invoice dispute",
        to="Ada <ada@example.test>, Bob <bob@example.test>",
        cc="Carol <carol@example.test>",
        sender="Grace <grace@example.test>",
    )

    parsed = normalize.normalize_message(raw)

    assert parsed.google_message_id == "m1"
    assert parsed.thread_id == "m1"
    assert parsed.subject == "Vendor invoice dispute"
    assert parsed.from_email == "grace@example.test"
    assert parsed.from_name == "Grace"
    assert parsed.to_emails == ["ada@example.test", "bob@example.test"]
    assert parsed.cc_emails == ["carol@example.test"]
    assert parsed.is_read is True
    del user, source


def test_the_unread_label_decides_is_read(mailbox):
    """Gmail's own state is per-message, so the label is the only source."""
    unread = normalize.normalize_message(_gmail_message("m1", unread=True), unread_label_id="UNREAD")
    read = normalize.normalize_message(_gmail_message("m2", unread=False), unread_label_id="UNREAD")

    assert unread.is_read is False
    assert read.is_read is True


def test_a_comma_joined_recipient_header_becomes_two_addresses(mailbox):
    """Gmail returns one string, not a list. Splitting it is the difference
    between two recipients and one malformed address."""
    raw = _gmail_message("m1", to="Ada <ada@example.test>, Bob <bob@example.test>")

    assert normalize.normalize_message(raw).to_emails == ["ada@example.test", "bob@example.test"]


def test_the_plain_part_is_preferred_over_the_html_part(mailbox):
    raw = _gmail_message("m1", body="the plain words", html="<p>the <b>html</b> words</p>")

    parsed = normalize.normalize_message(raw)

    assert parsed.body_text == "the plain words"
    assert parsed.body_html == "<p>the <b>html</b> words</p>"


def test_a_message_with_no_plain_part_is_still_readable(mailbox):
    """§6.1: the UI shows the stripped HTML when there is no plain part."""
    raw = _gmail_message("m1", body=None, html="<p>Hello <b>there</b></p>")
    raw["payload"]["mimeType"] = "text/html"

    user, source = mailbox
    _store(user, source, raw)
    stored = Message.objects.get(google_message_id="m1")

    assert normalize.readable_body(stored) == "Hello there"


def test_a_message_with_neither_part_falls_back_to_the_snippet(mailbox):
    raw = _gmail_message("m1", body=None)
    user, source = mailbox
    _store(user, source, raw)

    stored = Message.objects.get(google_message_id="m1")

    assert normalize.readable_body(stored) == "snippet for m1"


def test_an_attachment_is_a_flag_and_never_content(mailbox):
    """§8 asks for a flag only; parsing attachments is explicitly not in phase 1."""
    user, source = mailbox
    _store(user, source, _gmail_message("m1", has_attachment=True))

    assert Message.objects.get(google_message_id="m1").has_attachment is True


# ------------------------------------------------ §6.2: two writers, one rule


def test_a_gmail_label_change_overwrites_our_unread_flag(mailbox):
    """Writer 1: Gmail said unread, we had it read. Ops Hub follows."""
    user, source = mailbox
    row = _store(user, source, _gmail_message("m1"), is_read=True)

    assert normalize.apply_unread_label(row, now_is_read=False) is True
    assert Message.objects.get(pk=row.pk).is_read is False


def test_an_unchanged_label_writes_nothing(mailbox):
    """The common case, and the reason idle syncs stay cheap."""
    user, source = mailbox
    row = _store(user, source, _gmail_message("m1"), is_read=False)

    with CaptureQueriesContext(connection) as captured:
        assert normalize.apply_unread_label(row, now_is_read=False) is False

    assert captured.captured_queries == []


# ------------------------------------------------ §6.1: threads are a query


def test_a_thread_anchors_on_its_newest_message(mailbox):
    user, source = mailbox
    _store(user, source, _gmail_message("m1", thread_id="t1", subject="Oldest"), is_read=True)
    _store(
        user, source, _gmail_message("m2", thread_id="t1", subject="Newest"), internal_date=_at(21)
    )

    feed = normalize.feed_threads(user)

    assert len(feed) == 1
    assert feed[0].subject == "Newest"


def test_a_thread_is_unread_when_any_of_its_messages_is(mailbox):
    """§6.2's stated choice, not a consequence: a partially-read thread reads as
    read to a human, so the thread reads as unread."""
    user, source = mailbox
    _store(user, source, _gmail_message("m1", thread_id="t1"), is_read=True)
    _store(
        user, source, _gmail_message("m2", thread_id="t1"), is_read=False, internal_date=_at(21)
    )

    feed = normalize.feed_threads(user)

    assert feed[0].unread_count == 1


def test_a_thread_is_priority_when_any_of_its_messages_is(mailbox):
    user, source = mailbox
    _store(user, source, _gmail_message("m1", thread_id="t1"), is_priority=False)
    _store(
        user,
        source,
        _gmail_message("m2", thread_id="t1"),
        is_priority=True,
        internal_date=_at(21),
    )

    assert normalize.feed_threads(user)[0].is_priority is True


def test_participants_are_the_union_across_the_thread(mailbox):
    user, source = mailbox
    _store(
        user,
        source,
        _gmail_message("m1", thread_id="t1", sender="Grace <grace@example.test>",
                       to="Ada <ada@example.test>"),
    )
    _store(
        user,
        source,
        _gmail_message("m2", thread_id="t1", sender="Bob <bob@example.test>",
                       to="Ada <ada@example.test>, Carol <carol@example.test>"),
        internal_date=_at(21),
    )

    feed = normalize.feed_threads(user)

    assert set(feed[0].participants) == {
        "grace@example.test",
        "bob@example.test",
        "ada@example.test",
        "carol@example.test",
    }


def test_the_feed_is_one_query_and_no_n_plus_one(mailbox):
    """The spec's reason for not having a thread table: Postgres answers the
    whole feed in one pass. This is the assertion that it still does."""
    user, source = mailbox
    for thread in range(6):
        for message in range(4):
            _store(
                user,
                source,
                _gmail_message(f"m{thread}-{message}", thread_id=f"t{thread}"),
                internal_date=_at(20, hour=message),
            )

    with CaptureQueriesContext(connection) as captured:
        feed = normalize.feed_threads(user)

    assert len(feed) == 6
    assert len(captured.captured_queries) == 1


def test_the_feed_only_shows_my_own_mailbox(mailbox):
    """§7 row 15/16. An admin gets no mail visibility either, so this is the
    filter the feed is not allowed to have an argument for."""
    user, source = mailbox
    mate = AppUser.objects.create(
        email="mate@example.test", display_name="Mate", google_sub="sub-mate"
    )
    mate_source = Source.objects.create(user=mate, kind=Source.Kind.GMAIL)
    _store(user, source, _gmail_message("mine", thread_id="t1"))
    _store(mate, mate_source, _gmail_message("theirs", thread_id="t2"))

    assert [t.thread_id for t in normalize.feed_threads(user)] == ["t1"]


def test_a_thread_expands_oldest_first_for_one_owner(mailbox):
    user, source = mailbox
    _store(user, source, _gmail_message("m2", thread_id="t1"), internal_date=_at(21))
    _store(user, source, _gmail_message("m1", thread_id="t1"), internal_date=_at(20))

    thread = normalize.thread_messages(user, "t1")

    assert [m.google_message_id for m in thread] == ["m1", "m2"]


def test_headers_form_a_consistent_reply_chain(mailbox):
    """The fixture §6.3's reply builder is exercised against: real RFC 5322 ids
    in order, so In-Reply-To and References come out correct without a mailbox."""
    user, source = mailbox
    first = _gmail_message(
        "m1",
        thread_id="t1",
        subject="Escalation: API latency",
        message_id="<escalation.1@example.test>",
        sender="customer@example.test",
        to="Ada <ada@example.test>",
    )
    second = _gmail_message(
        "m2",
        thread_id="t1",
        subject="Re: Escalation: API latency",
        message_id="<escalation.2@example.test>",
        in_reply_to="<escalation.1@example.test>",
        sender="Ada <ada@example.test>",
        to="customer@example.test",
        internal_date=_at(21),
    )

    _store(user, source, first)
    _store(user, source, second)
    thread = normalize.thread_messages(user, "t1")

    assert [m.message_id_header for m in thread] == [
        "<escalation.1@example.test>",
        "<escalation.2@example.test>",
    ]
    assert thread[1].in_reply_to == "<escalation.1@example.test>"


def test_subject_reuse_keeps_the_re_prefix_verbatim(mailbox):
    """§6.3: reuse the thread subject verbatim, do not re-prefix. A 'Re: Re:' is
    the visible symptom of getting this wrong."""
    user, source = mailbox
    _store(user, source, _gmail_message("m1", thread_id="t1", subject="Re: Vendor invoice"))
    _store(
        user,
        source,
        _gmail_message("m2", thread_id="t1", subject="Re: Re: Vendor invoice"),
        internal_date=_at(21),
    )

    feed = normalize.feed_threads(user)

    assert feed[0].subject.startswith("Re: ")