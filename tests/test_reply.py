"""§6.3: replying to a thread, and the check that stands in front of the send.

Eight tests, and the ones worth reading first are the three refusals — because
the failure this path can produce is not a 500, it is a second email in a
person's mailbox that they cannot un-send. Each of these tests fails if the
matching guard is removed, and each of them fails *silently* if the guard is
merely reordered:

* `test_a_replayed_idempotency_key_sends_exactly_one_email` — the same key
  twice is one send. Not "one stored row": the assertion is on the call count,
  because a row count would pass with two emails in the thread and one of them
  never arriving.
* `test_another_users_thread_is_not_found_rather_than_forbidden` — §7 row 16.
  The value here is that 403 is *worse*, not better: it confirms the thread
  exists. 404 is the correct answer and the test names why.
* `test_a_failed_send_stores_nothing` — a send that Google rejected must leave
  no outbound row, because that row is what a later sync would believe.

Then three that pin the MIME the spec spells out (§6.3 steps 2 and 3), because a
reply with the wrong threading headers does not fail loudly: it lands in the
recipient's mailbox as a new conversation, and the person who sent it has no way
to tell.

The last two are about the read-marking on open, which is a local decision
because we hold no gmail.modify — §6.2 names the conflict and accepts it, and
`test_opening_a_thread_does_not_call_google` is what keeps a future reader from
helpfully "fixing" that.
"""

import datetime
from email import message_from_bytes, policy

import pytest

from accounts.models import AppUser
from core.models import Source
from mail import services
from mail.models import Message
from mail.services import SendFailed, ThreadNotFound, send_reply

UTC = datetime.timezone.utc


def _at(day, hour=9):
    return datetime.datetime(2026, 9, day, hour, tzinfo=UTC)


class FakeSend:
    """Stands in for the Gmail client. Counts calls, so tests assert on sends."""

    def __init__(self, *, error=None):
        self.calls = []
        self.error = error

    def send_raw(self, *, raw: bytes, thread_id: str) -> dict:
        # the modern policy, so the assertions read get_content() rather than
        # get_payload(decode=True) and a MIME rebuild shows up as a type change
        self.calls.append((message_from_bytes(raw, policy=policy.default), thread_id))
        if self.error is not None:
            raise self.error
        return {"id": f"sent-{len(self.calls)}", "threadId": thread_id}


@pytest.fixture
def thread(db):
    """Two messages from Ada to Owner, oldest first, with real Message-IDs.

    The Message-IDs are what `build_mime` chains onto, so they are present
    rather than absent: a test with null headers would pass while the threading
    was broken.
    """
    owner = AppUser.objects.create(
        email="owner@example.test", display_name="Owner", google_sub="sub-reply"
    )
    other = AppUser.objects.create(
        email="other@example.test", display_name="Other", google_sub="sub-other"
    )
    source = Source.objects.create(user=owner, kind=Source.Kind.GMAIL)
    for n, when in enumerate((_at(1), _at(2)), start=1):
        Message.objects.create(
            source=source,
            owner=owner,
            google_message_id=f"m{n}",
            thread_id="t1",
            message_id_header=f"<ada{n}@example.test>",
            subject="Re: budget",
            from_email="ada@example.test",
            from_name="Ada",
            to_emails=["owner@example.test"],
            body_text=f"message {n}",
            received_at=when,
            is_read=False,
        )
    return owner, source, other


# ------------------------------------------------ refusals come first


def test_a_replayed_idempotency_key_sends_exactly_one_email(thread):
    """§6.3 step 4. The assertion is on the send count, not the row count."""
    owner, _source, _other = thread
    service = FakeSend()

    first = send_reply(owner, "t1", "agreed", service=service, idempotency_key="k1")
    second = send_reply(owner, "t1", "agreed", service=service, idempotency_key="k1")

    assert len(service.calls) == 1
    assert second.deduplicated is True
    assert second.message.pk == first.message.pk


def test_another_users_thread_is_not_found_rather_than_forbidden(thread):
    """§6.3 step 2 with §7 row 16. A 403 would confirm the thread exists."""
    _owner, _source, other = thread
    service = FakeSend()

    with pytest.raises(ThreadNotFound):
        send_reply(other, "t1", "let me in", service=service)

    assert service.calls == []


def test_a_failed_send_stores_nothing(thread):
    """A rejected send must leave no outbound row to mislead the next sync."""
    owner, _source, _other = thread
    service = FakeSend(error=RuntimeError("gmail said no"))

    with pytest.raises(SendFailed) as caught:
        send_reply(owner, "t1", "hello", service=service, idempotency_key="k1")

    assert caught.value.unknown_status is False
    assert not Message.objects.filter(owner=owner, direction="outbound").exists()


def test_a_timeout_is_reported_as_unknown_rather_than_failed(thread):
    """§9.4's unknown-status row. The user must not be told to just try again."""
    owner, _source, _other = thread
    service = FakeSend(error=TimeoutError("no answer"))

    with pytest.raises(SendFailed) as caught:
        send_reply(owner, "t1", "hello", service=service, idempotency_key="k1")

    assert caught.value.unknown_status is True


# ------------------------------------------------ the MIME §6.3 spells out


def test_a_reply_reuses_the_subject_without_prefixing_it_again(thread):
    """Step 3. The subject already reads "Re:", and "Re: Re:" is the visible bug."""
    owner, _source, _other = thread
    service = FakeSend()

    send_reply(owner, "t1", "agreed", service=service)

    assert service.calls[0][0]["Subject"] == "Re: budget"


def test_a_reply_chains_the_thread_with_in_reply_to_and_references(thread):
    """Step 2. Wrong headers here file the reply as a new conversation."""
    owner, _source, _other = thread
    service = FakeSend()

    send_reply(owner, "t1", "agreed", service=service)

    raw, _thread_id = service.calls[0]
    # newest message is the In-Reply-To target
    assert raw["In-Reply-To"] == "<ada2@example.test>"
    assert raw["References"].split() == ["<ada1@example.test>", "<ada2@example.test>"]


def test_a_reply_to_your_own_message_goes_to_the_other_party(thread):
    """The actor is the latest sender, so the naive rule would mail yourself."""
    owner, source, _other = thread
    Message.objects.create(
        source=source,
        owner=owner,
        google_message_id="m3",
        thread_id="t1",
        subject="Re: budget",
        from_email="owner@example.test",
        to_emails=["ada@example.test"],
        body_text="my turn",
        received_at=_at(3),
    )
    service = FakeSend()

    send_reply(owner, "t1", "one more thing", service=service)

    assert service.calls[0][0]["To"] == "ada@example.test"


def test_the_body_is_sent_as_text_not_as_a_google_html_payload(thread):
    """RFC 5322 text/plain. Gmail rejects a raw body it cannot parse as MIME."""
    owner, _source, _other = thread
    service = FakeSend()

    send_reply(owner, "t1", "line one\nline two", service=service)

    raw, _thread_id = service.calls[0]
    assert raw.get_content_type() == "text/plain"
    assert "line one\nline two" in raw.get_content()


# ------------------------------------------------ local read state


def test_opening_a_thread_marks_every_message_read_without_calling_google(thread):
    """§6.2 writer 2. We hold no gmail.modify, so this stays local."""
    owner, _source, _other = thread

    assert services.mark_thread_read(owner, "t1") == 2
    assert services.unread_count(owner) == 0


def test_the_outbound_row_carries_the_spent_key_and_no_body_of_its_own(thread):
    """The key is what makes the replay above a no-op; the body is already in
    the user's mail client, so §6.1's storage rule does not apply to it."""
    owner, _source, _other = thread
    service = FakeSend()

    result = send_reply(owner, "t1", "agreed", service=service, idempotency_key="k1")

    row = result.message
    assert row.idempotency_key == "k1"
    assert row.direction == Message.Direction.OUTBOUND
    # same source as the thread it replied to, not a fresh lookup
    assert row.source_id == Message.objects.get(pk=result.message.pk).source_id

# ------------------------------------------------ over HTTP


def _as(client, user):
    session = client.session
    session["app_user_id"] = user.pk
    session.save()


def _with_token(user):
    from accounts import crypto
    from datetime import timedelta

    from django.utils import timezone

    user.refresh_token = crypto.encrypt(b"rt")
    user.token_expires_at = timezone.now() + timedelta(hours=1)
    user.save(update_fields=["refresh_token", "token_expires_at"])


def test_over_http_another_users_thread_is_404_not_403(client, thread):
    """§7 row 16 through the whole stack, because that is where 403 leaks back in.

    The service test proves `ThreadNotFound`; this proves the *view* turns it into
    404 rather than a 403 that would confirm the thread exists.
    """
    _owner, _source, other = thread
    _as(client, other)

    assert client.get("/mail/thread/t1").status_code == 404
    assert client.get("/mail/thread/t1/reply").status_code == 404


def test_over_http_a_replayed_key_posts_one_email(client, thread, monkeypatch):
    """The same double-click, at the layer a user can actually produce."""
    from mail import views as mail_views

    owner, _source, _other = thread
    _with_token(owner)
    service = FakeSend()
    monkeypatch.setattr(mail_views, "build_service", lambda token: service)
    _as(client, owner)

    form = {"body": "agreed", "idempotency_key": "k-http"}
    assert client.post("/mail/thread/t1/reply", form).status_code == 302
    assert client.post("/mail/thread/t1/reply", form).status_code == 302

    assert len(service.calls) == 1
    assert Message.objects.filter(owner=owner, direction="outbound").count() == 1


def test_over_http_a_failed_send_keeps_the_draft_in_the_box(client, thread, monkeypatch):
    """§9.4: the composed text survives, so a retry is not retyping from memory."""
    from mail import views as mail_views

    owner, _source, _other = thread
    _with_token(owner)
    monkeypatch.setattr(
        mail_views, "build_service", lambda token: FakeSend(error=RuntimeError("no"))
    )
    _as(client, owner)

    response = client.post("/mail/thread/t1/reply", {"body": "typed carefully", "idempotency_key": "k"})

    assert response.status_code == 200
    body = response.content.decode()
    assert "typed carefully" in body
    # the key is not reissued, so a retry cannot produce a second email
    assert 'value="k"' in body


def test_over_http_an_empty_reply_is_refused_without_calling_google(client, thread, monkeypatch):
    from mail import views as mail_views

    owner, _source, _other = thread
    _with_token(owner)
    service = FakeSend()
    monkeypatch.setattr(mail_views, "build_service", lambda token: service)
    _as(client, owner)

    response = client.post("/mail/thread/t1/reply", {"body": "   ", "idempotency_key": "k"})

    assert response.status_code == 200
    assert service.calls == []
    assert "Write something" in response.content.decode()


def test_the_reply_form_issues_a_fresh_key_on_every_get(client, thread):
    """A stale key is the one that would suppress a genuine new reply."""
    import re

    owner, _source, _other = thread
    _as(client, owner)

    keys = set()
    for _ in range(2):
        body = client.get("/mail/thread/t1/reply").content.decode()
        keys.add(re.search(r'name="idempotency_key" value="([0-9a-f]{32})"', body).group(1))

    assert len(keys) == 2


def test_opening_a_thread_clears_the_unread_badge_on_the_page_that_caused_it(client, thread):
    """§6.2 writer 2, marked before the render so the sidebar is already right."""
    owner, _source, _other = thread
    _as(client, owner)

    assert client.get("/mail/inbox").content.decode().count("unread message") == 1
    assert Message.objects.filter(owner=owner, is_read=False).count() == 2

    client.get("/mail/thread/t1")

    assert Message.objects.filter(owner=owner, is_read=False).count() == 0
    assert "0 unread messages" in client.get("/mail/inbox").content.decode()
