"""The mailboxes and thread pages over HTTP. No query lives in this module.

Same rule as `tasks.views`, and for the same reason: §7 rule 0 makes the view
layer hold no permission logic, and the CI grep that fails the build on a query
here is what holds it.

**Every refusal in this module is a 404, never a 403, on a mail route.** §7 row
16 makes mail private to its owner at every role, so a 403 would answer the
question "does thread t_1 exist and is it not mine?" — which is the only question
an attacker needs. A member asking for a mailbox or thread they do not own gets
exactly the same bytes as a member asking for one that was never there.

The idempotency key is minted per GET of the reply form and travels through the
POST. That is what makes a double-click, a resubmit after a timeout, and a
browser back-then-forward all one email rather than three; the service checks it
before `messages.send`, and the form carries a fresh one every time it renders.
"""

from __future__ import annotations

import uuid

from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from accounts.services import refresh
from accounts.services.tokens import read_access_token
from core.permissions import login_required
from mail.normalize import feed_threads, readable_body, thread_messages
from mail.management.commands.sync_gmail import build_service
from mail.services import (
    SendFailed,
    ThreadNotFound,
    mark_thread_read,
    send_reply,
    unread_count,
)

FEED_LIMIT = 50


def _mailbox_for(actor, kind: str, label: str):
    """The actor's Source row of this kind, or 404.

    The filter carries the user id as well as the kind, so a member naming
    another mailbox's id gets the same 404 as a member naming nothing. An
    unconnected source still renders — with an empty feed and a banner — because
    "you have not connected this yet" is a thing the user needs to be able to
    see, and hiding it behind a 404 would leave them with no route to fix it.
    """
    source = actor.sources.filter(kind=kind).first()
    if source is None:
        raise Http404("no such mailbox") from None
    return source


def _banner(request, source):
    """The connection banner, or None when there is nothing to say.

    Reads `request.session["sync_notice"]` rather than the Source row's error
    columns: the notice has to survive the redirect that follows the callback
    and the token refresh, and a request-scoped value is the only thing that
    does. One-shot, so a stale "could not sync" cannot sit on the page forever.
    """
    return request.session.pop("sync_notice", None)


@login_required
@require_http_methods(["GET"])
def inbox(request):
    """Unread mail. One query for the feed, one COUNT for the unread badge."""
    source = _mailbox_for(request.actor, "gmail", "Mail")
    return render(
        request,
        "mail/inbox.html",
        {
            "source": source,
            "threads": feed_threads(request.actor, limit=FEED_LIMIT),
            "unread": unread_count(request.actor),
            "notice": _banner(request, source),
        },
    )


@login_required
@require_http_methods(["GET"])
def thread(request, thread_id: str):
    """One thread, oldest first, marked read on open (§6.2 writer 2).

    The mark happens on GET, before the render, so the unread badge in the
    sidebar is already correct on the page that caused the change rather than
    one navigation later.
    """
    messages = thread_messages(request.actor, thread_id)
    if not messages:
        raise Http404("no such thread") from None
    mark_thread_read(request.actor, thread_id)
    return render(
        request,
        "mail/thread.html",
        {"messages": _with_bodies(messages), "thread_id": thread_id},
    )


def _with_bodies(messages) -> list[dict]:
    """Pair each message with the text the page shows.

    Here and not in the template because a Django template cannot call a
    function with an argument, and `readable_body` needs one. Doing it here also
    keeps §6.1's "never render body_html" decision in one readable line instead
    of spread across a template expression.
    """
    return [{"message": m, "body": readable_body(m)} for m in messages]


@login_required
@require_http_methods(["GET", "POST"])
def reply(request, thread_id: str):
    """Send a reply. The form GET mints the key; the POST spends it.

    Three states, and the third is the one that matters:

    * GET — an empty box and a fresh idempotency key in the hidden field.
    * POST, key already spent — the service returns the first reply without
      calling Gmail, and this redirects exactly as a fresh send does. The user
      sees one reply in their thread, which is the truth.
    * POST, send failed — re-render with the typed text still in the box. §9.4's
      "unknown outcome" copy differs from a plain failure, because blind retry
      of an unknown-status send is how people mail the same person twice.
    """
    if request.method == "GET":
        messages = thread_messages(request.actor, thread_id)
        if not messages:
            raise Http404("no such thread") from None
        return render(
            request,
            "mail/reply.html",
            {
                "messages": _with_bodies(messages),
                "thread_id": thread_id,
                "idempotency_key": uuid.uuid4().hex,
            },
        )

    body = (request.POST.get("body") or "").strip()
    key = request.POST.get("idempotency_key") or None

    if not body:
        messages = thread_messages(request.actor, thread_id)
        if not messages:
            raise Http404("no such thread") from None
        return render(
            request,
            "mail/reply.html",
            {
                "messages": _with_bodies(messages),
                "thread_id": thread_id,
                "idempotency_key": key or uuid.uuid4().hex,
                "error": "Write something before sending.",
                "draft": "",
            },
        )

    # Same token path as both sync commands, so a user whose grant died finds
    # out here the same way they would have in the scheduler. §4.1's scope set
    # already includes gmail.send; no extra grant is requested here.
    outcome = refresh.refresh_if_needed(request.actor)
    if outcome not in (refresh.OUTCOME_REFRESHED, refresh.OUTCOME_SKIPPED):
        return _needs_reauth(request, outcome, thread_id)

    try:
        send_reply(
            request.actor,
            thread_id,
            body,
            service=build_service(read_access_token(request.actor)),
            idempotency_key=key,
        )
    except ThreadNotFound:
        raise Http404("no such thread") from None
    except SendFailed as exc:
        messages = thread_messages(request.actor, thread_id)
        if not messages:
            raise Http404("no such thread") from None
        return render(
            request,
            "mail/reply.html",
            {
                "messages": _with_bodies(messages),
                "thread_id": thread_id,
                "idempotency_key": key or uuid.uuid4().hex,
                # unknown_status keeps the spent key: retrying the same form
                # must not produce a second email if the first one did land.
                "error": (
                    "We could not confirm your reply sent. Check the thread before sending again."
                    if exc.unknown_status
                    else str(exc)
                ),
                "draft": body,
            },
        )

    return HttpResponseRedirect(reverse("mail_thread", args=[thread_id]))


def _needs_reauth(request, outcome: str, thread_id: str) -> HttpResponse:
    """Store the reason and bounce to sign-in. Reconnecting is the user's fix.

    A deferred refresh (Google had a bad moment) is not the user's fault and is
    not a reconnect prompt, so it says what actually happened and goes back to
    the form with the draft intact. Only needs_reauth asks for consent.
    """
    if outcome == refresh.OUTCOME_DEFERRED:
        return HttpResponseRedirect(reverse("mail_reply", args=[thread_id]))
    request.session["sync_notice"] = "Your Google connection needs reconnecting."
    return HttpResponseRedirect(reverse("auth_google_start"))