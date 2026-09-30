"""The one function that decides who may write to a task.

Model B (§7 rows 8, 8a, 9, 10) lives in `editable_by` and nowhere else. It is
used from inside the WHERE clause of the statements that read and write the
row, so the actor's identity travels into the SQL as a bind value and never
becomes a branch in Python. Three consequences the design is worth having:

* A read-then-check leaves a window. Two concurrent requests both read the old
  assignee, both decide they may write, and both write. The predicate is
  evaluated against the row by the same statements that act on it, so there is
  no window to lose and no cached decision to go stale.
* A view that checks first is a second code path, and the second code path is
  the one somebody forgets. A caller here can only pass the actor; it cannot
  widen access by accident, because the row that was not permitted is the row
  that did not change.
* A refused write changes nothing at all. It surfaces as NotAllowed, which the
  view layer turns into 403, and it is indistinguishable from a task that does
  not exist — no partial write, no half-applied reassignment.

The pre-edit assignee is the load-bearing detail. `editable_by` compares
against the row as the database holds it, so a member cannot satisfy row 8 by
first reassigning a colleague's task to themselves: the check was already made
against the previous owner. A check made in Python after the write would pass
that very first time, and the task would be stolen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.core.paginator import Page, Paginator
from django.db.models import Q
from django.db.models.expressions import RawSQL
from django.utils import timezone

from accounts.models import AppUser
from audit.writer import record
from core.permissions import require_role
from tasks.models import Task

# Fifty rows is a cap rather than a preference: the board is the one list in
# this app that grows without bound as the team works, and a list whose length
# is the length of the table is a page that eventually times out.
BOARD_PAGE_SIZE = 50
DUE_SOON_LIMIT = 10
SCOPE_MINE = "mine"
SCOPE_TEAM = "team"
OPEN_STATUSES = (Task.Status.TODO, Task.Status.IN_PROGRESS)

# The columns a board page is allowed to read, named rather than defaulted.
# `select_related("assignee")` on its own is `SELECT task.*, app_user.*`, which
# drags a colleague's encrypted token columns into the result set to print a
# display name next to them. §7 row 2 says no path returns another user's token
# columns, and a name is the only thing this page wanted, so the name is all
# that is asked for.
CARD_FIELDS = (
    "id",
    "title",
    "status",
    "priority",
    "due_at",
    "assignee_id",
    "assignee__display_name",
)
DETAIL_FIELDS = CARD_FIELDS + (
    "description",
    "completed_at",
    "created_at",
    "updated_at",
    "created_by_id",
    "assignee__email",
    "created_by__display_name",
)


class NotAllowed(Exception):
    """The actor may not touch that task. Row 403 at the view layer."""


class NotFound(Exception):
    """There is no such task. Row 404 at the view layer."""


def _audit_value(value):
    """Datetimes as text, because the audit metadata column is JSON.

    Not cosmetic: a value the JSON encoder refuses is an error raised inside
    the caller's transaction, and a failed statement ends it. An audit trail is
    never worth that, so the caller hands over something writable.
    """
    return value.isoformat() if isinstance(value, datetime) else value


def _everyone_or_nobody(condition: str, *params) -> Q:
    """A WHERE term that admits every row or none, decided by `condition`.

    A Q compares a *column* to a value, and two of the facts Model B must test —
    the actor's role and the assignee this write would produce — are values, not
    columns. Comparing the pk against a bound switch is how both get said
    without a branch in Python. The switch is MAX(id)+1 rather than a literal so
    the comparison is true of every row that exists, not only of the row whose
    id happens to match.
    """
    switch = (
        f"SELECT CASE WHEN {condition}"
        " THEN (SELECT COALESCE(MAX(id), 0) + 1 FROM task) ELSE 0 END"
    )
    return Q(pk__lte=RawSQL(switch, params))


def editable_by(actor: AppUser, new_assignee_id: int | None = None) -> Q:
    """Model B, as a WHERE fragment over the row and the write being attempted.

    Three branches, which are §7 rows 8, 8a and the admin escape, in that order:
    the row is mine; the row is unclaimed *and* this edit keeps it mine or leaves
    it unclaimed; the actor is an admin. Nothing else admits anything, so the
    authority is decided against the row the database holds at write time rather
    than against a snapshot the caller carried in.

    The obvious-looking alternative is a bug, not a shorter version of this.
    `Q(assignee_id__isnull=require_role(actor, 'admin'))` reads like the spec and
    silently inverts the predicate: Django prunes a no-op Q, so a member's third
    branch is dropped rather than becoming false, leaving every member an editor
    of every task, while an admin is left unable to edit anything they do not own.
    tests/test_permissions.py::test_task_edit_predicate_is_sql_and_only_selects_what_the_actor_owns
    is the test that notices.
    """
    return (
        _everyone_or_nobody("%s = 'admin'", actor.role)
        | Q(assignee_id=actor.pk)
        | (
            Q(assignee_id__isnull=True)
            & _everyone_or_nobody(
                "%s IS NULL OR %s = %s", new_assignee_id, new_assignee_id, actor.pk
            )
        )
    )


def edit_task(actor: AppUser | None, task_id: int, **changes) -> Task:
    """Apply `changes` to one task, or raise NotAllowed having changed nothing.

    Two gated statements rather than one, because the predicate reads a column
    the write overwrites: the first states the actor's authority over the row as
    it is and carries the pre-edit values with it, the second performs the
    write under the same predicate. The second gate is what makes this safe
    rather than merely careful — if anything moved the row between the two
    statements, the write refuses and the exception is honest.
    """
    if actor is None:
        raise NotAllowed("no actor")

    if "assignee" in changes:  # the same column, the other spelling
        changes["assignee_id"] = changes.pop("assignee").pk

    gate = editable_by(actor, changes.get("assignee_id"))
    prior = (
        Task.objects.filter(gate)
        .filter(pk=task_id)
        .values("assignee_id", "status", "completed_at")
        .first()
    )
    if prior is None:
        raise NotAllowed("no such task this actor may edit")

    next_values = dict(changes)
    if next_values.get("status", prior["status"]) == Task.Status.DONE:
        next_values["completed_at"] = prior["completed_at"] or timezone.now()
    else:
        next_values["completed_at"] = None

    written = Task.objects.filter(gate).filter(pk=task_id).update(**next_values)
    if written == 0:
        raise NotAllowed("the row changed under the edit")

    record(
        actor,
        "task.update",
        "task",
        target_id=task_id,
        metadata={"prior_assignee_id": prior["assignee_id"], "changed": sorted(next_values)},
    )
    return Task.objects.get(pk=task_id)


def delete_task(actor: AppUser | None, task_id: int) -> None:
    """Hard delete, admins only (row 9). The prior row goes into the audit entry.

    Unlike the edit path this one really is a role check rather than a row
    predicate, because there is no field on the task that would express "this
    row may be removed" differently for an admin and a member: row 9 grants it
    to the role and withholds it from everyone else.
    """
    if not require_role(actor, AppUser.Role.ADMIN):
        raise NotAllowed("delete is admin only")

    prior = Task.objects.filter(pk=task_id).values().first()
    if prior is None:
        raise NotAllowed("no such task")

    Task.objects.filter(pk=task_id).delete()
    record(
        actor,
        "task.delete",
        "task",
        target_id=task_id,
        metadata={name: _audit_value(value) for name, value in prior.items()},
    )


def delete_tasks(actor: AppUser | None, task_ids: list[int]) -> int:
    """Bulk delete, the same admin guard applied per id (row 10).

    There is no blanket DELETE with an unfiltered id list anywhere in this
    module, and the guard is re-derived from the actor on every call rather
    than carried over from an earlier one.
    """
    deleted = 0
    for task_id in task_ids:
        delete_task(actor, task_id)
        deleted += 1
    return deleted


def create_task(
    actor: AppUser | None,
    *,
    title: str,
    status: str,
    priority: str,
    description: str = "",
    due_at=None,
    assignee_id: int | None = None,
) -> Task:
    """File a new task with `actor` as its creator, whatever the request said.

    Row 7 makes the creator the actor's own id, and row 8a's rule is applied
    to the row being born: a member may file a task for themselves or leave it
    for anyone to claim, and naming a third party is the same act as claiming
    one and handing it straight on, so it takes an admin exactly as it does on
    an update. Nothing about a fresh row needs the created_by override, because
    there is no field a client could put it on.

    The vocabularies and the lengths are deliberately not re-checked here. They
    are CHECK constraints and varchar bounds in the table (tasks/models.py), so
    no caller can store a value the database would refuse; the form in the view
    layer is what turns a bad one into a message rather than an exception.
    """
    if actor is None:
        raise NotAllowed("no actor")
    if assignee_id is not None and assignee_id != actor.pk and not require_role(actor, AppUser.Role.ADMIN):
        raise NotAllowed("a member may only create a task for themselves")

    return Task.objects.create(
        created_by=actor,
        assignee_id=assignee_id,
        title=title,
        description=description or None,
        priority=priority,
        status=status,
        due_at=due_at,
    )


# ------------------------------------------------------------------ board reads
# Everything below is a read. None of it filters on who may look, because §7
# row 6 says the board is the shared surface: a member sees every task and the
# write-scoped half of the model is expressed by which controls the page
# offers, not by which rows it renders.


def board_page(
    owner_id: int,
    *,
    page: int = 1,
    scope: str = SCOPE_MINE,
    status: str | None = None,
) -> Page:
    """The board, 50 rows to a page, with an out-of-range page clamped not refused.

    `owner_id` rather than a whole user row, because the only thing the caller
    contributes to the query is the id it is scoped to; passing a row would
    widen the signature without widening the answer. The team scope filters on
    nothing, which is row 6 saying out loud.

    A page number past the end answers with the last real page. Somebody who
    followed a stale bookmark or typed 9999 gets a board instead of a
    traceback, and nothing is lost by it: the page numbers carry no state.
    """
    if scope not in (SCOPE_MINE, SCOPE_TEAM):
        scope = SCOPE_MINE
    if status not in Task.Status.values:
        status = None

    rows = Task.objects.all()
    if scope == SCOPE_MINE:
        rows = rows.filter(assignee_id=owner_id)
    if status is not None:
        rows = rows.filter(status=status)
    # select_related so a page that prints the assignee's name is one query
    # rather than one per row, and only() so that one query does not read the
    # columns nobody here prints (CARD_FIELDS says which). It joins app_user,
    # which is the other side of a many-to-one and adds no filter of its own,
    # so the task indexes below are still the ones that serve this query.
    rows = rows.select_related("assignee").only(*CARD_FIELDS)
    # status then due_at is the order both indexes are shaped for: the composite
    # (assignee_id, status) leads with this scope's filter, and (status, due_at)
    # leads with the status filter. due_at ascending leaves undated work last,
    # which is Postgres' own NULLS LAST rather than a decision made here.
    paginator = Paginator(rows.order_by("status", "due_at", "pk"), BOARD_PAGE_SIZE)
    return paginator.get_page(max(1, min(page, paginator.num_pages)))


def due_soon(limit: int = DUE_SOON_LIMIT) -> list[Task]:
    """The due-soon column: the team's open work with a date, nearest first.

    Bounded on purpose — it is a column beside the list, and a column with no
    limit is an unbounded query on every page render. Team-wide rather than
    per-user for the same reason the list has a team scope: row 6.
    """
    return list(
        Task.objects.filter(status__in=OPEN_STATUSES)
        .exclude(due_at=None)
        .select_related("assignee")
        .only(*CARD_FIELDS)
        .order_by("due_at", "pk")[:limit]
    )


def directory() -> list[AppUser]:
    """The team, as far as the assignee picker is allowed to know it (row 3).

    Name, email, role. The token columns are excluded in the query rather than
    hidden by a template, so no later page change can surface one. No actor is
    taken because row 3 grants the directory to every member and there is
    nothing here for an actor to widen; a pre-created row with no google_sub is
    an invitation rather than a user (§7) and is not on the board yet.
    """
    return list(
        AppUser.objects.filter(google_sub__isnull=False, is_active=True)
        .order_by("display_name")
        .only("id", "display_name", "email", "role")
    )


def assignee_choices(actor: AppUser, task: Task | None = None) -> list[tuple[str, str]]:
    """Who this actor may name on this task, as the picker's own options.

    These are the rule `edit_task` enforces, read the same way round, so nobody
    is offered a choice the write will refuse. An admin may name anyone (row 8);
    the assignee may hand their own task to anyone (row 8a); anyone else looking
    at an unclaimed task may take it or leave it unclaimed, which is the whole
    of the carve-out and is why the unassigned option comes first.
    """
    if require_role(actor, AppUser.Role.ADMIN) or (task is not None and task.assignee_id == actor.pk):
        people = directory()
    else:
        people = [actor]
    return [("", "Unassigned")] + [(str(p.pk), f"{p.display_name} ({p.email})") for p in people]


@dataclass(frozen=True)
class TaskDetail:
    """One task and the two permissions its page needs in order to render.

    A small view-model rather than a flag the template guesses at, because the
    flags are decisions. `editable` is row 8 answered by the same SQL predicate
    that guards the write, so a page cannot offer an Edit control for a write
    the service would refuse.
    """

    task: Task
    editable: bool
    deletable: bool


def task_detail(actor: AppUser | None, task_id: int) -> TaskDetail:
    """One task, with the controls its page is allowed to offer.

    Row 6 is why this returns a colleague's task to a member rather than
    hiding it: the board is read-shared, so the predicate is asked as a question
    instead of being used as a filter. Filtering here would make a task the
    actor is entitled to read disappear, and a member who can see a task on the
    board must be able to open it.
    """
    task = (
        Task.objects.select_related("assignee", "created_by")
        .only(*DETAIL_FIELDS)
        .filter(pk=task_id)
        .first()
    )
    if task is None:
        raise NotFound("no such task")

    # An unresolved session is not a member, so it gets the read row 6 grants
    # everyone and none of the controls — the same "no actor, no privilege"
    # rule require_role already applies to the delete flag.
    editable = actor is not None and Task.objects.filter(editable_by(actor), pk=task_id).exists()
    return TaskDetail(
        task=task,
        editable=editable,
        deletable=require_role(actor, AppUser.Role.ADMIN),
    )
