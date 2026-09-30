"""The permission choke point, and the §7 matrix it is the choke point for.

Two halves, deliberately in one file because they are the same subject:

1. MATRIX below — a transcription of the spec's §7 table, one entry per row,
   including the two rows a reviewer is told to push back on (8/8a and 16) and
   the two nobody does (24, 25). A row added to the spec gets an entry here in
   the same commit; the completeness test below fails, naming the row, if it
   does not. Each entry names the step that will enforce it, so an unfinished
   row is visibly unfinished instead of quietly absent.

2. The decorators. The headline behaviour is that a member on an admin-only
   route gets 403 and not 302 — a 302 would tell an authenticated caller that
   the route exists, which is a free map of the privileged surface.

The probe views and urlpatterns below exist only for these tests. The
decorators are still read-only in production: no shipped view wears one yet, so
the proof is a test-only URLconf rather than a route invented to be measured.

3. Model B, the task write predicate, proved against the matrix it implements.
   The one test to read first is
   `test_task_edit_taking_a_colleagues_task_by_reassigning_it_to_self`: it is
   the case a read-then-check implementation fails, and it is the reason the
   predicate is a WHERE fragment rather than a helper. Every test in the
   `task_edit` group is named for what the spec says must happen, not for the
   function it calls.
"""

from dataclasses import dataclass

import pytest
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import DataError, IntegrityError, connection, transaction
from django.http import HttpRequest, HttpResponse
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path

from accounts.models import AppUser
from accounts.views import google_start
from core.permissions import (
    SESSION_USER_KEY,
    admin_required,
    login_required,
    require_member,
    require_role,
)
from tasks.models import Task
from tasks.services import NotAllowed, delete_task, delete_tasks, edit_task, editable_by


@dataclass(frozen=True)
class MatrixRow:
    number: str
    resource: str
    action: str
    admin: bool
    member: bool
    enforcement: str
    step: str | None  # the step that enforces this row; None once it is enforced


# Spec §7, in spec order. The column pair is the spec's own answer, including
# the rows that are No for everybody — those are the ones that get "fixed" by
# accident when someone reads a table quickly.
MATRIX: list[MatrixRow] = [
    MatrixRow("1", "app_user", "Sign in via Google", True, True, "public /auth/google", None),
    MatrixRow("2", "app_user", "Read own profile, own tokens", True, True, "WHERE id = actor.id", "24"),
    MatrixRow("3", "app_user", "Read team directory", True, True, "full list, tokens excluded", "16"),
    MatrixRow("4", "app_user", "Change another user's role", True, False, "require_role -> 403", "24"),
    MatrixRow("5", "app_user", "Read audit log", True, False, "require_role -> 403", "24"),
    MatrixRow("6", "task", "List / read", True, True, "no owner filter", "16"),
    MatrixRow("7", "task", "Create", True, True, "created_by_id forced server-side", "16"),
    MatrixRow("8", "task", "Update", True, False, "Model B, in the UPDATE WHERE", None),
    MatrixRow("8a", "task", "Reassign to a different user", True, False, "Model B, pre-edit assignee", None),
    MatrixRow("9", "task", "Delete", True, False, "require_role -> 403", None),
    MatrixRow("10", "task", "Bulk delete", True, False, "guard per id", None),
    MatrixRow("11", "calendar_event", "Read own", True, True, "WHERE owner_id = actor.id", "24"),
    MatrixRow("12", "calendar_event", "Read another user's (title, times)", True, True, "no description", "24"),
    MatrixRow("13", "calendar_event", "Create / update own", True, True, "owner_id in the WHERE", "19"),
    MatrixRow("14", "calendar_event", "Update / delete another user's", True, False, "require_role -> 403", "24"),
    MatrixRow("15", "message", "List / read own inbox", True, True, "WHERE owner_id = actor.id", "24"),
    MatrixRow("16", "message", "Read another user's mailbox", False, False, "404, never 403", "24"),
    MatrixRow("17", "message", "Mark read/unread, toggle priority", True, True, "owner_id in the WHERE", "24"),
    MatrixRow("18", "message", "Send reply", True, True, "the actor's own token", "22"),
    MatrixRow("19", "source", "Read own sync status", True, True, "WHERE user_id = actor.id", "24"),
    MatrixRow("20", "source", "Read all users' sync status", True, False, "require_role -> 403", "24"),
    MatrixRow("21", "source", "Force resync", True, True, "members: own source_id only", "24"),
    MatrixRow("22", "source", "Disconnect an integration", True, True, "members: own only", "24"),
    MatrixRow("23", "audit_log", "Read", True, False, "require_role -> 403", "24"),
    MatrixRow("24", "audit_log", "Write", False, False, "audit/writer.py only", None),
    MatrixRow("25", "audit_log", "Update / delete", False, False, "Postgres triggers raise", None),
]

# Rows this repo has already enforced somewhere, with the test that proves each.
# A row lands here only when that proof exists and passes.
ALREADY_ENFORCED = {
    "1": "tests/test_signin_gate.py::test_claim_refusal_is_visible_to_the_view_and_the_audit_trail",
    "8": "tests/test_permissions.py::test_task_edit_of_a_colleagues_task_is_refused_and_changes_nothing",
    "8a": "tests/test_permissions.py::test_task_edit_of_an_unassigned_task_to_a_third_party_is_refused",
    "9": "tests/test_permissions.py::test_task_edit_delete_by_a_member_is_refused",
    "10": "tests/test_permissions.py::test_task_edit_bulk_delete_by_a_member_removes_nothing",
    "24": "tests/test_audit.py::test_audit_writer_drops_secret_metadata",
    "25": "tests/test_audit.py::test_audit_log_rejects_update_and_delete",
}

# The numbering §7 uses, 8a included. Listed literally on purpose: a new row in
# the spec has exactly one place to be added, and MATRIX above stops matching
# the moment it is forgotten.
SPEC_ROW_NUMBERS = [
    "1", "2", "3", "4", "5", "6", "7", "8", "8a", "9", "10", "11", "12", "13",
    "14", "15", "16", "17", "18", "19", "20", "21", "22", "23", "24", "25",
]


# ------------------------------------------------------------- matrix integrity


@pytest.mark.parametrize("number", [row.number for row in MATRIX])
def test_matrix_row_names_a_step_or_names_its_proof(number):
    """Every row is either enforced somewhere (with the proof named) or owed to a step."""
    row = next(r for r in MATRIX if r.number == number)
    if row.step is None:
        assert number in ALREADY_ENFORCED, f"row {number} claims to be enforced with no proof named"
    else:
        assert number not in ALREADY_ENFORCED, f"row {number} is enforced and still owes step {row.step}"


def test_matrix_covers_every_row_the_spec_numbers():
    """A row added to §7 without an entry here fails, naming the row."""
    assert [row.number for row in MATRIX] == SPEC_ROW_NUMBERS


# ------------------------------------------------------------------- decorators


@login_required
def member_probe(request):
    return HttpResponse(f"member page for {request.actor.email}")


@admin_required
def admin_probe(request):
    return HttpResponse(f"admin page for {request.actor.email}")


# The sign-in route is carried into the probe urlconf because the decorators
# redirect by name: an unresolvable name is a loud failure, and swallowing it
# with a hardcoded path would hide a renamed route behind a working redirect.
urlpatterns = [
    path("auth/google", google_start, name="auth_google_start"),
    path("probe/member", member_probe),
    path("probe/admin", admin_probe),
]


def _signed_in_as(client, user) -> None:
    session = client.session
    session[SESSION_USER_KEY] = user.pk
    session.save()


def _member(**overrides) -> AppUser:
    fields = dict(email="mem@example.com", display_name="Member", google_sub="sub-m", role="member")
    return AppUser.objects.create(**{**fields, **overrides})


def _admin() -> AppUser:
    return AppUser.objects.create(
        email="boss@example.com", display_name="Boss", google_sub="sub-a", role=AppUser.Role.ADMIN
    )


def _request_carrying(rf, **session) -> HttpRequest:
    """A request with a real SessionStore, not a dict standing in for one."""
    request = rf.get("/probe/member")
    SessionMiddleware(lambda _req: HttpResponse()).process_request(request)
    for key, value in session.items():
        request.session[key] = value
    request.session.save()
    return request


@override_settings(ROOT_URLCONF=__name__)
def test_rule0_login_decorator_sends_an_anonymous_caller_to_sign_in(client, db):
    response = client.get("/probe/member")

    assert response.status_code == 302
    assert response["Location"] == "/auth/google"


@override_settings(ROOT_URLCONF=__name__)
def test_rule0_login_decorator_admits_a_member_and_hands_the_view_the_actor(client, db):
    member = _member()
    _signed_in_as(client, member)

    response = client.get("/probe/member")

    assert response.status_code == 200
    assert b"mem@example.com" in response.content


@override_settings(ROOT_URLCONF=__name__)
def test_rule0_admin_decorator_gives_a_member_403_and_not_302(client, db):
    """The load-bearing case: 302 here would confirm the route exists to a member."""
    member = _member()
    _signed_in_as(client, member)

    response = client.get("/probe/admin")

    assert response.status_code == 403
    assert not response.has_header("Location")


@override_settings(ROOT_URLCONF=__name__)
def test_rule0_admin_decorator_admits_an_admin_and_hands_the_view_the_actor(client, db):
    admin = _admin()
    _signed_in_as(client, admin)

    response = client.get("/probe/admin")

    assert response.status_code == 200
    assert b"boss@example.com" in response.content


# ---------------------------------------------------------------- role vocabulary


def test_a_session_naming_an_invitation_row_is_not_a_member(rf, db):
    """§7: a pre-created row is an invitation, not a user, and cannot sign in."""
    invited = AppUser.objects.create(email="invite@example.com", display_name="Invited")

    request = _request_carrying(rf, **{SESSION_USER_KEY: invited.pk})

    assert require_member(request) is None


def test_a_deactivated_row_is_not_a_member(rf, db):
    user = _member(is_active=False)

    request = _request_carrying(rf, **{SESSION_USER_KEY: user.pk})

    assert require_member(request) is None


def test_a_stale_session_naming_a_deleted_row_resolves_to_nobody(rf, db):
    request = _request_carrying(rf, **{SESSION_USER_KEY: 999999})

    assert require_member(request) is None


def test_lead_is_not_an_admin_because_no_spec_row_makes_it_one(db):
    """§7 grants admin powers to 'admin' and to nothing else."""
    lead = _member(email="lead@example.com", google_sub="sub-l", role=AppUser.Role.LEAD)

    assert require_role(lead, AppUser.Role.ADMIN) is False
    assert require_role(lead, AppUser.Role.LEAD) is True


def test_no_actor_holds_no_privilege(db):
    assert require_role(None, AppUser.Role.ADMIN) is False


# ------------------------------------------------------------- model B, §7 rows
# 8, 8a, 9 and 10. The group name (`task_edit`) is the spec's word: it selects
# the whole task write surface, and the proof is `-k "task_edit"`.


def _person(email: str, role: str = AppUser.Role.MEMBER) -> AppUser:
    return AppUser.objects.create(
        email=email, display_name=email, google_sub=f"sub-{email}", role=role
    )


def _task(created_by: AppUser, assignee: AppUser | None = None, **extra) -> Task:
    return Task.objects.create(
        created_by=created_by, assignee=assignee, **{"title": "vendor contract", **extra}
    )


def _writable_state(task: Task) -> tuple:
    """Every field an edit could touch, so "nothing changed" is checkable."""
    task.refresh_from_db()
    return (
        task.title,
        task.description,
        task.assignee_id,
        task.priority,
        task.status,
        task.due_at,
        task.completed_at,
    )


def test_task_edit_by_an_admin_of_a_task_assigned_to_a_member_is_allowed(db):
    """§10 row 8: admin, 200, changed."""
    member = _person("member@x.example")
    admin = _person("boss@x.example", AppUser.Role.ADMIN)
    task = _task(created_by=admin, assignee=member, title="before")

    edit_task(admin, task.pk, title="after")

    task.refresh_from_db()
    assert task.title == "after"


def test_task_edit_by_the_assignee_of_their_own_task_is_allowed(db):
    """§10 row 8: the ordinary case — the assignee edits their own task."""
    member = _person("member@x.example")
    task = _task(created_by=member, assignee=member, title="before")

    edit_task(member, task.pk, title="after", priority=Task.Priority.URGENT)

    task.refresh_from_db()
    assert (task.title, task.priority) == ("after", Task.Priority.URGENT)


def test_task_edit_of_a_colleagues_task_is_refused_and_changes_nothing(db):
    """§10 row 8: member, 403, zero rows changed. Row 8 and 8a in one test."""
    owner = _person("owner@x.example")
    colleague = _person("colleague@x.example")
    task = _task(created_by=owner, assignee=owner, title="before")
    before = _writable_state(task)

    with pytest.raises(NotAllowed):
        edit_task(colleague, task.pk, title="after")

    assert _writable_state(task) == before


def test_task_edit_claims_an_unassigned_task_by_assigning_it_to_self(db):
    """§10 row 8: the unassigned carve-out is what keeps a task from being a dead row."""
    member = _person("member@x.example")
    task = _task(created_by=member, assignee=None)

    edit_task(member, task.pk, assignee_id=member.pk)

    task.refresh_from_db()
    assert task.assignee_id == member.pk


def test_task_edit_of_an_unassigned_task_to_a_third_party_is_refused(db):
    """§10 row 8a: claiming means claiming, not appointing somebody else."""
    member = _person("member@x.example")
    third = _person("third@x.example")
    task = _task(created_by=member, assignee=None)

    with pytest.raises(NotAllowed):
        edit_task(member, task.pk, assignee_id=third.pk)

    task.refresh_from_db()
    assert task.assignee_id is None


def test_task_edit_hands_an_owned_task_to_someone_else_is_allowed(db):
    """§10 row 8a: the assignee may give their own task away."""
    owner = _person("owner@x.example")
    heir = _person("heir@x.example")
    task = _task(created_by=owner, assignee=owner)

    edit_task(owner, task.pk, assignee_id=heir.pk)

    task.refresh_from_db()
    assert task.assignee_id == heir.pk


def test_task_edit_taking_a_colleagues_task_by_reassigning_it_to_self_is_refused(db):
    """The row a read-then-check implementation fails, and the reason it fails.

    A member who fetches the task, decides it is theirs, and writes
    `assignee = me` passes every check that happens after the read. The
    predicate is evaluated against the row by the statement that writes it, so
    the answer is no — and the task is still the colleague's afterwards.
    """
    owner = _person("owner@x.example")
    taker = _person("taker@x.example")
    task = _task(created_by=owner, assignee=owner, title="before")

    with pytest.raises(NotAllowed):
        edit_task(taker, task.pk, assignee_id=taker.pk, title="mine now")

    assert _writable_state(task) == ("before", None, owner.pk, "normal", "todo", None, None)


def test_task_edit_is_refused_when_the_row_moved_under_the_caller(db):
    """The predicate reads the row, not the caller's snapshot of it.

    Whatever the caller last saw, the authority is decided against what the
    database holds at the moment of the write. Two members claiming the same
    unassigned task is the real version of this race: the loser was told it was
    theirs a moment ago, and must be refused now.
    """
    owner = _person("owner@x.example")
    slow = _person("slow@x.example")
    fast = _person("fast@x.example")
    task = _task(created_by=owner, assignee=None)

    Task.objects.filter(pk=task.pk).update(assignee_id=fast.pk)

    with pytest.raises(NotAllowed):
        edit_task(slow, task.pk, assignee_id=slow.pk)
    task.refresh_from_db()
    assert task.assignee_id == fast.pk


def test_task_edit_predicate_is_sql_and_only_selects_what_the_actor_owns(db):
    """Two halves of one claim: the predicate is SQL, and it is the SQL that ran.

    The first half is asserted against the statement that reached Postgres
    rather than against the source, so a refactor that moved the check back
    into Python fails here even when every behavioural test still passes. The
    second half pins the width of the predicate, which is what a no-op Q breaks
    silently: Django drops a branch that is always true or always false, and a
    member's permission quietly becomes the entire table.
    """
    member = _person("member@x.example")
    other = _person("other@x.example")
    mine = _task(created_by=member, assignee=member)
    theirs = _task(created_by=member, assignee=other)
    unassigned = _task(created_by=member, assignee=None)

    with CaptureQueriesContext(connection) as captured:
        edit_task(member, mine.pk, title="after")

    update = next(q for q in captured.captured_queries if q["sql"].startswith("UPDATE"))
    assert f'"task"."assignee_id" = {member.pk}' in update["sql"]
    assert '"task"."assignee_id" IS NULL' in update["sql"]
    # the role rides in as a value, which is why there is no branch in Python
    assert "SELECT CASE WHEN 'member' = 'admin'" in update["sql"]

    assert set(Task.objects.filter(editable_by(member)).values_list("pk", flat=True)) == {
        mine.pk,
        unassigned.pk,
    }


def test_task_edit_of_a_task_that_does_not_exist_is_refused(db):
    """A row that is not there and a row the actor may not touch answer the same."""
    member = _person("member@x.example")

    with pytest.raises(NotAllowed):
        edit_task(member, 999_999, title="after")


def test_task_edit_by_nobody_is_refused(db):
    """An unresolved session is not a member. Nothing reaches the database."""
    owner = _person("owner@x.example")
    task = _task(created_by=owner, assignee=owner, title="before")

    with pytest.raises(NotAllowed):
        edit_task(None, task.pk, title="after")

    task.refresh_from_db()
    assert task.title == "before"


def test_task_edit_delete_by_a_member_is_refused(db):
    """§7 row 9: delete is the admin's alone, on any task."""
    member = _person("member@x.example")
    task = _task(created_by=member, assignee=member)

    with pytest.raises(NotAllowed):
        delete_task(member, task.pk)

    assert Task.objects.filter(pk=task.pk).exists()


def test_task_edit_delete_by_an_admin_removes_the_row(db):
    admin = _person("boss@x.example", AppUser.Role.ADMIN)
    task = _task(created_by=admin, assignee=admin)

    delete_task(admin, task.pk)

    assert not Task.objects.filter(pk=task.pk).exists()


def test_task_edit_bulk_delete_by_a_member_removes_nothing(db):
    """§7 row 10: the guard is per id, so a refusal is not partial either."""
    member = _person("member@x.example")
    first = _task(created_by=member, assignee=member)
    second = _task(created_by=member, assignee=member)

    with pytest.raises(NotAllowed):
        delete_tasks(member, [first.pk, second.pk])

    assert Task.objects.count() == 2


def test_task_edit_bulk_delete_by_an_admin_removes_only_the_listed_rows(db):
    admin = _person("boss@x.example", AppUser.Role.ADMIN)
    first = _task(created_by=admin, assignee=admin)
    second = _task(created_by=admin, assignee=admin)
    untouched = _task(created_by=admin, assignee=admin)

    assert delete_tasks(admin, [first.pk, second.pk]) == 2

    assert list(Task.objects.values_list("pk", flat=True)) == [untouched.pk]


# ---------------------------------------------------------- what the schema owns
# The vocabularies and bounds are constraints, so these are asserted against
# the database: a raw statement is refused exactly like an ORM write, which is
# the whole reason they are constraints rather than form cleanups.


def test_task_constraint_refuses_a_blank_title_from_raw_sql(db):
    owner = _person("owner@x.example")

    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "insert into task (title, priority, status, created_by_id, created_at, updated_at)"
                " values ('   ', 'normal', 'todo', %s, now(), now())",
                [owner.pk],
            )


def test_task_constraint_refuses_a_vocabulary_the_scheduler_never_reads(db):
    owner = _person("owner@x.example")
    task = _task(created_by=owner, assignee=owner)

    with pytest.raises(IntegrityError), transaction.atomic():
        Task.objects.filter(pk=task.pk).update(priority="whenever")

    with pytest.raises(IntegrityError), transaction.atomic():
        Task.objects.filter(pk=task.pk).update(status="almost")

    task.refresh_from_db()
    assert (task.priority, task.status) == (Task.Priority.NORMAL, Task.Status.TODO)


def test_task_constraint_refuses_a_description_past_its_bound(db):
    owner = _person("owner@x.example")

    with pytest.raises(DataError), transaction.atomic():
        Task.objects.create(created_by=owner, title="long one", description="x" * 20_001)
