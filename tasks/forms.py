"""The task form, for both the create and the edit page.

A plain `Form` rather than a `ModelForm`, for one reason: a ModelForm would
save the row itself, and the only thing allowed to write a task is
`tasks/services.py` under the Model B predicate. This module turns a request
into validated values and a set of options; it never touches the database
except to read the option list the service hands it.

The vocabularies are generated from the model rather than written out again,
so a value added to `Task.Status` appears in the form in the same commit that
added it. The bounds that the table enforces anyway — 200 characters of title,
20,000 of description — are repeated here only so a person is told before the
database refuses, not because the form is what makes them safe.
"""

from __future__ import annotations

from django import forms

from tasks.models import MAX_DESCRIPTION, MAX_TITLE, Task

# The browser's own datetime-local control speaks this and nothing else, so the
# parser is told one format rather than a list of every one a person might type.
DUE_INPUT_FORMAT = "%Y-%m-%dT%H:%M"


class TaskForm(forms.Form):
    """Validated task fields, plus an assignee list the service chose.

    `assignee_choices` is required rather than defaulted: the picker's options
    are a permission decision (see `tasks.services.assignee_choices`), so there
    is no sensible "everyone" fallback to reach for when a caller forgets.
    """

    def __init__(self, *args, assignee_choices: list[tuple[str, str]], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["assignee"].choices = assignee_choices

    title = forms.CharField(max_length=MAX_TITLE, label="Title")
    description = forms.CharField(
        required=False, max_length=MAX_DESCRIPTION, widget=forms.Textarea(attrs={"rows": 4})
    )
    status = forms.ChoiceField(choices=Task.Status.choices)
    priority = forms.ChoiceField(choices=Task.Priority.choices)
    due_at = forms.DateTimeField(
        required=False,
        input_formats=[DUE_INPUT_FORMAT],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format=DUE_INPUT_FORMAT),
        help_text="Optional. Shown in UTC.",
    )
    # The empty option is "unassigned", which is a real state on this board and
    # not an absence: an unclaimed task is the row every member may take.
    assignee = forms.ChoiceField(required=False)

    def task_fields(self) -> dict:
        """The service's keyword arguments, from validated data only.

        `created_by` is not in here and cannot be: there is no field for it, so
        a request that posts one has it dropped at this boundary rather than
        ignored somewhere further in (§7 row 7).
        """
        data = self.cleaned_data
        return {
            "title": data["title"],
            "description": data["description"],
            "status": data["status"],
            "priority": data["priority"],
            "due_at": data["due_at"],
            "assignee_id": int(data["assignee"]) if data["assignee"] else None,
        }
