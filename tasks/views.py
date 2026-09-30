"""The task board over HTTP. No query lives in this module.

That is not tidiness — §7 rule 0 makes it the enforcement mechanism, and the CI
grep that fails the build on a query here is what holds it. A view reads the
request, hands `request.actor` to a service, and renders what comes back.

Which refusal is which, for tasks specifically, because the resource decides
it. A read is shared (row 6): a member opening a colleague's task gets 200 and
a page whose edit controls are simply absent, and a task that is not there is
404. There is no existence to hide on a page everybody may read, so nothing to
gain by hiding one. A write is scoped (row 8), and on a write route the
refusal is at the door rather than on save: an edit form for a row the caller
may not touch is that row's contents in the wrong reader's hands before the
403 ever arrives. A missing task is still 404 on those routes, because the
member was allowed to ask.
"""

from __future__ import annotations

from django.http import Http404, HttpResponse, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from core.permissions import admin_required, login_required
from tasks.forms import TaskForm
from tasks.models import Task
from tasks.services import (
    SCOPE_MINE,
    NotAllowed,
    NotFound,
    assignee_choices,
    board_page,
    create_task,
    delete_task,
    directory,
    due_soon,
    edit_task,
    task_detail,
)

READ = ["GET", "HEAD"]
READ_WRITE = ["GET", "HEAD", "POST"]


def _page_number(raw: str) -> int:
    """A page number that is a number. Anything a person typed is page one."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1


def _refused() -> HttpResponse:
    """The 403 for a write Model B refused. Plain text, and it names no field."""
    return HttpResponse(
        "You do not have permission to change that task.",
        status=403,
        content_type="text/plain; charset=utf-8",
    )


def _initial(task) -> dict:
    """A task as the form's initial data.

    `assignee_id` rather than `assignee` because the id is already on the row
    that was fetched, and the relation is not: reading the relation here would
    be a query per field per form.
    """
    return {
        "title": task.title,
        "description": task.description or "",
        "status": task.status,
        "priority": task.priority,
        "due_at": task.due_at,
        "assignee": str(task.assignee_id) if task.assignee_id else "",
    }


def _form(request, task=None, data=None, initial=None) -> TaskForm:
    """The form, with the assignee options the service says this actor may pick.

    Building it here rather than in the service is deliberate: a form is a view
    concern, and the options it is given are the answer to a question about the
    actor, which is the service's job. The service does not need to know a form
    exists to answer it.
    """
    return TaskForm(data, initial=initial, assignee_choices=assignee_choices(request.actor, task))


@login_required
@require_http_methods(READ)
def board(request):
    """The board: 50 rows a page, my work by default and the team's on request.

    The two scopes are the two shapes the indexes were built for, and the
    filters are passed through rather than re-derived: the vocabulary check
    belongs with the query that uses it.
    """
    return render(
        request,
        "tasks/board.html",
        {
            "page": board_page(
                request.actor.pk,
                page=_page_number(request.GET.get("page", 1)),
                scope=request.GET.get("scope", SCOPE_MINE),
                status=request.GET.get("status") or None,
            ),
            "scope": request.GET.get("scope", SCOPE_MINE),
            "status": request.GET.get("status", ""),
            "statuses": Task.Status.choices,
            "due_soon": due_soon(),
        },
    )


@login_required
@require_http_methods(READ)
def detail(request, task_id: int):
    """One task, with the controls its page is allowed to offer (rows 6 and 8).

    A task that is not there is 404, said in as many words by the module
    docstring: row 6 makes this resource readable by everyone, so there is no
    existence to hide and no reason to spend a 403 on one.
    """
    try:
        shown = task_detail(request.actor, task_id)
    except NotFound:
        raise Http404("no such task") from None
    return render(request, "tasks/detail.html", {"detail": shown})


@login_required
@require_http_methods(READ_WRITE)
def create(request):
    """File a task. The creator is the actor, whatever the form was sent."""
    if request.method == "POST":
        form = _form(request, data=request.POST)
        if form.is_valid():
            created = create_task(request.actor, **form.task_fields())
            return HttpResponseRedirect(reverse("task_detail", args=[created.pk]))
    else:
        form = _form(request)

    return render(
        request,
        "tasks/form.html",
        {"form": form, "heading": "New task", "submit_label": "Create task"},
    )


@login_required
@require_http_methods(READ_WRITE)
def edit(request, task_id: int):
    """Edit a task, when Model B says this actor may.

    Fail closed, and the reason is step 14's: a member must not be able to probe
    for the existence of a route or a resource they cannot touch. A 200 with a
    populated form is that probe — the row's title, description and assignee
    have already reached a reader who has no authority over it — so the refusal
    is before the form is built, not on save. The detail page omits the Edit
    link for the same reason, which leaves the typed URL as the only way here.

    The authority is the service's answer, read off the row `task_detail`
    already fetched. Nothing about roles or ownership is restated here, so
    there is one predicate in this codebase and this is not a second copy of it.

    The 404 below is an existence check and nothing more: row 6 makes the board
    readable by everyone, so a member asking for a row that is not there is
    told it is not there, and a member asking for one that is there and not
    theirs is refused.
    """
    try:
        shown = task_detail(request.actor, task_id)
    except NotFound:
        raise Http404("no such task") from None

    if not shown.editable:
        return _refused()

    if request.method == "POST":
        form = _form(request, task=shown.task, data=request.POST)
        if form.is_valid():
            try:
                edit_task(request.actor, task_id, **form.task_fields())
            except NotAllowed:
                return _refused()
            return HttpResponseRedirect(reverse("task_detail", args=[task_id]))
    else:
        form = _form(request, task=shown.task, initial=_initial(shown.task))

    return render(
        request,
        "tasks/form.html",
        {"form": form, "heading": f"Edit #{task_id}", "submit_label": "Save task"},
    )


@login_required
@require_http_methods(["POST"])
def claim(request, task_id: int):
    """Take an unclaimed task. The same predicate as any other edit (row 8).

    No special case for the unassigned row: `edit_task` admits an unclaimed
    task when the write names the actor, and refuses it when the write names
    somebody else, so this route cannot become a way around row 8a.
    """
    try:
        edit_task(request.actor, task_id, assignee_id=request.actor.pk)
    except NotAllowed:
        return _refused()
    return HttpResponseRedirect(reverse("task_detail", args=[task_id]))


@admin_required
@require_http_methods(["POST"])
def delete(request, task_id: int):
    """Remove a task (row 9). The decorator has already answered a member.

    A refusal that reaches here is the only one left: the caller is an admin,
    so `delete_task` declined because there was no such row.
    """
    try:
        delete_task(request.actor, task_id)
    except NotAllowed:
        raise Http404("no such task") from None
    return HttpResponseRedirect(reverse("task_board"))


@login_required
@require_http_methods(READ)
def team(request):
    """The team directory (row 3): names, emails, roles, and no token columns."""
    return render(request, "tasks/directory.html", {"people": directory()})
