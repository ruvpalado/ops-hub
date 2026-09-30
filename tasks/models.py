"""The task board's row. One task, optionally assigned to one person.

The vocabularies and the length limits live in CHECK constraints rather than in
a form or a serializer. `status` and `priority` are read back by queries with
no Python in the loop — the scheduler and the board's due-soon column both
filter in SQL — so a value the database would never recognise has to be
impossible to store in the first place.

The two indexes are named after the two real query patterns in §7, not guessed
at: the "my tasks" default (assignee first, status second) and the board's
open-tasks-by-due-date column. The write path needs no index of its own: §7 row
8 resolves through the primary key before the permission predicate is
evaluated.
"""

from __future__ import annotations

from django.db import models

from accounts.models import AppUser

PRIORITIES = ("low", "normal", "high", "urgent")
STATUSES = ("todo", "in_progress", "done")
MAX_TITLE = 200
MAX_DESCRIPTION = 20_000


class Task(models.Model):
    class Priority(models.TextChoices):
        LOW = "low"
        NORMAL = "normal"
        HIGH = "high"
        URGENT = "urgent"

    class Status(models.TextChoices):
        TODO = "todo"
        IN_PROGRESS = "in_progress"
        DONE = "done"

    # The 200 bound is varchar(200), enforced by Postgres itself; the CHECK
    # below adds only the part a column type cannot say, which is that the
    # title is not blank. §3.2's `length(btrim(title)) between 1 and 200` is
    # half a column type and half a constraint, and this is the split.
    title = models.CharField(max_length=MAX_TITLE)
    # varchar(20000) rather than text + a length CHECK: Django's ORM cannot
    # express a check against a function's result, so the bound is expressed
    # as the column type, which Postgres enforces the same way. Nothing here
    # depends on the difference — a varchar reads and sorts like a text.
    description = models.CharField(max_length=MAX_DESCRIPTION, blank=True, null=True)
    # SET NULL on delete: removing a user releases their tasks back to the
    # unassigned pool rather than deleting work someone else still needs.
    # db_index=False because the composite (assignee, status) index below
    # already leads with this column, and Postgres will not use a two-column
    # index for a one-column lookup anyway — so a second index here would be
    # a duplicate of the index that matters.
    assignee = models.ForeignKey(
        AppUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=False,
        related_name="assigned_tasks",
    )
    # RESTRICT on delete: the creator of a task is the reason it exists, so the
    # user row cannot go out from under it (§7 row 9). Also unindexed: this
    # column is only read by audit-adjacent questions at a table size where a
    # sequential scan is the faster plan, and §3.2's own note says so.
    created_by = models.ForeignKey(
        AppUser, on_delete=models.PROTECT, db_index=False, related_name="created_tasks"
    )
    priority = models.CharField(max_length=16, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.TODO)
    due_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # §3.2 names this table `task`, and so does every query anyone will
        # write against it by hand. Django's default would be `tasks_task`.
        db_table = "task"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(title__regex=r"^\s*\S"), name="task_title_not_blank"
            ),
            models.CheckConstraint(
                condition=models.Q(priority__in=PRIORITIES), name="task_priority_valid"
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=STATUSES), name="task_status_valid"
            ),
        ]
        indexes = [
            # "my tasks": WHERE assignee_id = ? ORDER BY status — §7 row 8 also
            # makes this the set of rows this user is allowed to write.
            models.Index(fields=["assignee", "status"], name="task_assignee_status_idx"),
            # the board's due-soon column: WHERE status = ? ORDER BY due_at, one
            # index serving both the filter and the sort.
            models.Index(fields=["status", "due_at"], name="task_status_due_at_idx"),
        ]

    def __str__(self) -> str:
        return f"#{self.pk} {self.title}"
