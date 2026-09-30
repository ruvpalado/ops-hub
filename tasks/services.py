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

from datetime import datetime

from django.db.models import Q
from django.db.models.expressions import RawSQL
from django.utils import timezone

from accounts.models import AppUser
from audit.writer import record
from core.permissions import require_role
from tasks.models import Task


class NotAllowed(Exception):
    """The actor may not touch that task. Row 403 at the view layer."""


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
