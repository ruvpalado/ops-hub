"""The one place a request becomes an actor, and the two decorators every view
wears. Spec §7 Rule 0: enforcement is structural, not per-view discipline — a
view holds no permission logic of its own because there is nothing for it to
hold; it reads request.actor and hands that to a service.

Two refusals, deliberately different answers:

* no session -> 302 to sign-in, because signing in is the caller's fix;
* signed in, wrong role -> 403, because it is not. Answering 302 there would
  tell an authenticated member that an admin-only route exists and that their
  own cookie somehow is not good enough, which is a free inventory of the
  privileged surface. 403 says "no" without describing what they did not get.

Only 'admin' is an admin. The spec's §3.1 check constraint admits two roles and
grants admin powers to exactly one; AppUser.Role.LEAD exists in the model but
no §7 row grants it anything, so it is treated as a non-admin here rather than
guessed at. Widening that is a spec amendment, not a code change.

Not django.contrib.auth.decorators.login_required: this app has no Django auth
user, and that decorator resolves to a User this project never sets. Import
this module qualified (core.permissions.admin_required) so the name collision
with Django's login_required is never a surprise.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse_lazy

from accounts.models import AppUser

# The session stores a bare AppUser pk and nothing else. It lives here, not in
# a view module, because it is the input to role resolution and every view that
# wears these decorators is its other consumer. accounts.views re-exports the
# name so the sign-in views that write it keep one definition, not two.
SESSION_USER_KEY = "app_user_id"

SIGN_IN_URL = reverse_lazy("auth_google_start")


def require_member(request: HttpRequest) -> AppUser | None:
    """The signed-in member behind this request, or None.

    "Member" is the spec's definition (§7), not merely "the session has a key":
    the row must carry a google_sub, because a pre-created row is an
    invitation that has never signed in and holds no tokens. The definition
    goes in the WHERE rather than a check after the fetch, so a stale or
    hand-set session cookie cannot widen anything.
    """
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        return None
    return AppUser.objects.filter(
        pk=user_id, google_sub__isnull=False, is_active=True
    ).first()


def require_role(actor: AppUser | None, role: str) -> bool:
    """True when the actor holds exactly this role.

    Written as a comparison rather than a membership test so step 15 can pass
    it into an UPDATE's WHERE as a query parameter: the spec's rule is that the
    actor's identity is a bind value, never a branch taken after the row is
    read. A None actor is False, so a denied operation updates zero rows and
    the caller turns that into a 403.
    """
    return actor is not None and actor.role == role


def _forbidden() -> HttpResponse:
    """A 403 that names nothing. The body must not confirm what the route is."""
    return HttpResponse(
        "You do not have permission to view this page.",
        status=403,
        content_type="text/plain; charset=utf-8",
    )


def login_required(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    """Any signed-in member. The default wrapper for a view in this app."""

    @wraps(view)
    def wrapper(request: HttpRequest, *args, **kwargs) -> HttpResponse:
        actor = require_member(request)
        if actor is None:
            return HttpResponseRedirect(str(SIGN_IN_URL))
        request.actor = actor
        return view(request, *args, **kwargs)

    return wrapper


def admin_required(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    """Members of either role, but a member who is not an admin gets 403.

    Refusals here are deliberately not audited. The spec names the denials that
    write an audit row (user.role_change, audit.read) and none of them is the
    generic gate, so adding a write here would put rows in the trail that no
    spec row asked for.
    """

    @wraps(view)
    def wrapper(request: HttpRequest, *args, **kwargs) -> HttpResponse:
        actor = require_member(request)
        if actor is None:
            return HttpResponseRedirect(str(SIGN_IN_URL))
        if not require_role(actor, AppUser.Role.ADMIN):
            return _forbidden()
        request.actor = actor
        return view(request, *args, **kwargs)

    return wrapper
