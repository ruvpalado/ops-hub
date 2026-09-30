import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.models import AppUser
from core.permissions import SESSION_USER_KEY
from tasks.models import Task

UTC = datetime.timezone.utc


@pytest.fixture
def people(db):
    owner = AppUser.objects.create(email="owner@example.com", display_name="Owner", google_sub="sub-o")
    mate = AppUser.objects.create(email="mate@example.com", display_name="Mate", google_sub="sub-m")
    admin = AppUser.objects.create(
        email="boss@example.com", display_name="Boss", google_sub="sub-a", role=AppUser.Role.ADMIN
    )
    return owner, mate, admin


def _as(client, user):
    session = client.session
    session[SESSION_USER_KEY] = user.pk
    session.save()


def test_board_is_fifty_rows_a_page_of_my_work(client, people):
    owner, _mate, _admin = people
    Task.objects.bulk_create(
        [Task(created_by=owner, assignee=owner, title=f"task {i}") for i in range(55)]
    )
    _as(client, owner)

    first = client.get("/tasks/")
    second = client.get("/tasks/?page=2")

    assert first.status_code == 200
    assert "task 0" in first.content.decode()
    assert "task 54" not in first.content.decode()
    assert "55 total" in first.content.decode()
    assert "page 1 of 2" in first.content.decode()
    assert "task 54" in second.content.decode()


def test_board_page_past_the_end_shows_the_last_page_not_a_traceback(client, people):
    owner, _mate, _admin = people
    Task.objects.bulk_create(
        [Task(created_by=owner, assignee=owner, title=f"task {i}") for i in range(55)]
    )
    _as(client, owner)

    response = client.get("/tasks/?page=9999")

    assert response.status_code == 200
    assert "page 2 of 2" in response.content.decode()


def test_board_escapes_a_task_title(client, people):
    owner, _mate, _admin = people
    Task.objects.create(created_by=owner, assignee=owner, title="<script>alert(1)</script>")
    _as(client, owner)

    body = client.get("/tasks/").content.decode()

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_a_member_reading_a_colleagues_task_gets_the_page_and_no_edit_controls(client, people):
    owner, mate, _admin = people
    theirs = Task.objects.create(created_by=owner, assignee=owner, title="owners work")
    _as(client, mate)

    response = client.get(f"/tasks/{theirs.pk}")
    body = response.content.decode()

    assert response.status_code == 200
    assert "owners work" in body
    assert "Claim this task" not in body
    assert f"/tasks/{theirs.pk}/edit" not in body


def test_a_member_reading_an_unclaimed_task_is_offered_the_claim(client, people):
    _owner, mate, _admin = people
    open_ = Task.objects.create(created_by=mate, title="up for grabs")
    _as(client, mate)

    body = client.get(f"/tasks/{open_.pk}").content.decode()

    assert "Claim this task" in body


def test_row_8_a_member_asking_for_a_colleagues_edit_form_is_refused_and_learns_nothing(client, people):
    """§7 row 8 at the door: the edit route closes before the form exists.

    The disclosure this guards is the whole row: a 200 with a populated form
    hands the title, the description and the assignee to a reader with no
    authority over the task, and hands it to anyone who can type a URL. So the
    assertion is not only the status code — it is that not one field value from
    that row appears in what comes back.
    """
    owner, mate, _admin = people
    theirs = Task.objects.create(
        created_by=owner,
        assignee=owner,
        title="quarterly headcount freeze",
        description="salary bands stay closed until the audit is signed off",
        priority="high",
        status=Task.Status.IN_PROGRESS,
    )
    _as(client, mate)

    response = client.get(f"/tasks/{theirs.pk}/edit")
    body = response.content.decode()

    assert response.status_code == 403
    for disclosed in (
        theirs.title,
        theirs.description,
        "salary bands stay closed",
        owner.display_name,
        owner.email,
        theirs.priority,
        theirs.status,
    ):
        assert disclosed not in body, f"{disclosed!r} reached a reader who may not edit this task"


def test_row_8_an_assignee_is_still_given_their_own_edit_form(client, people):
    """The same route, opened by the person it belongs to: 200 and their form."""
    owner, mate, _admin = people
    mine = Task.objects.create(created_by=owner, assignee=mate, title="my own work")
    _as(client, mate)

    response = client.get(f"/tasks/{mine.pk}/edit")
    body = response.content.decode()

    assert response.status_code == 200
    assert "my own work" in body
    assert "<form" in body


def test_row_8_a_member_saving_a_colleagues_task_is_refused_and_the_row_is_unchanged(client, people):
    owner, mate, _admin = people
    theirs = Task.objects.create(created_by=owner, assignee=owner, title="owners work", priority="high")
    _as(client, mate)

    response = client.post(
        f"/tasks/{theirs.pk}/edit",
        {"title": "taken over", "status": "done", "priority": "low", "description": "", "assignee": str(mate.pk)},
    )

    assert response.status_code == 403
    theirs.refresh_from_db()
    assert theirs.title == "owners work"
    assert theirs.priority == "high"
    assert theirs.assignee_id == owner.pk
    assert theirs.status == Task.Status.TODO


def test_a_member_claiming_an_unclaimed_task_takes_it_for_themselves(client, people):
    _owner, mate, _admin = people
    open_ = Task.objects.create(created_by=mate, title="up for grabs")
    _as(client, mate)

    response = client.post(f"/tasks/{open_.pk}/claim")

    assert response.status_code == 302
    open_.refresh_from_db()
    assert open_.assignee_id == mate.pk


def test_a_member_claiming_a_task_somebody_else_owns_is_refused(client, people):
    owner, mate, _admin = people
    theirs = Task.objects.create(created_by=owner, assignee=owner, title="owners work")
    _as(client, mate)

    response = client.post(f"/tasks/{theirs.pk}/claim")

    assert response.status_code == 403
    theirs.refresh_from_db()
    assert theirs.assignee_id == owner.pk


def test_an_assignee_may_reassign_their_own_task_including_away_from_themselves(client, people):
    owner, mate, admin = people
    mine = Task.objects.create(created_by=owner, assignee=mate, title="mine")
    _as(client, mate)

    response = client.post(
        f"/tasks/{mine.pk}/edit",
        {"title": "mine", "status": "todo", "priority": "normal", "description": "", "assignee": str(admin.pk)},
    )

    assert response.status_code == 302
    mine.refresh_from_db()
    assert mine.assignee_id == admin.pk


def test_creating_a_task_records_the_actor_as_creator_however_the_form_is_sent(client, people):
    owner, mate, admin = people
    _as(client, mate)

    response = client.post(
        "/tasks/new",
        {
            "title": "filed by a member",
            "status": "todo",
            "priority": "high",
            "description": "",
            "assignee": str(mate.pk),
            "created_by_id": str(admin.pk),
        },
    )

    assert response.status_code == 302
    created = Task.objects.get(title="filed by a member")
    assert created.created_by_id == mate.pk
    assert created.assignee_id == mate.pk


def test_a_member_cannot_file_a_task_for_somebody_else(client, people):
    owner, mate, _admin = people
    _as(client, mate)

    response = client.post(
        "/tasks/new",
        {
            "title": "someone else's problem",
            "status": "todo",
            "priority": "normal",
            "description": "",
            "assignee": str(owner.pk),
        },
    )

    assert response.status_code == 200
    assert "someone else" in response.content.decode()
    assert not Task.objects.filter(title="someone else's problem").exists()


def test_an_admin_may_file_a_task_for_anybody(client, people):
    owner, _mate, admin = people
    _as(client, admin)

    response = client.post(
        "/tasks/new",
        {
            "title": "assigned by the boss",
            "status": "todo",
            "priority": "normal",
            "description": "",
            "assignee": str(owner.pk),
        },
    )

    assert response.status_code == 302
    assert Task.objects.get(title="assigned by the boss").assignee_id == owner.pk


def test_a_blank_title_is_refused_and_nothing_is_written(client, people):
    _owner, mate, _admin = people
    _as(client, mate)

    response = client.post("/tasks/new", {"title": "", "status": "todo", "priority": "normal", "assignee": ""})

    assert response.status_code == 200
    assert Task.objects.count() == 0


def test_deleting_a_task_is_admins_work(client, people):
    owner, mate, admin = people
    task = Task.objects.create(created_by=owner, assignee=mate, title="scratch")

    _as(client, mate)
    assert client.post(f"/tasks/{task.pk}/delete").status_code == 403
    assert Task.objects.filter(pk=task.pk).exists()

    _as(client, admin)
    response = client.post(f"/tasks/{task.pk}/delete")
    assert response.status_code == 302
    assert not Task.objects.filter(pk=task.pk).exists()


def test_a_task_that_is_not_there_is_a_404(client, people):
    owner, mate, admin = people
    _as(client, mate)
    assert client.get("/tasks/999999").status_code == 404

    _as(client, admin)
    assert client.post("/tasks/999999/delete").status_code == 404
    assert client.post("/tasks/999999/claim").status_code == 403


def test_the_due_soon_column_is_bounded_to_ten_nearest_first(client, people):
    owner, _mate, _admin = people
    base = datetime.datetime(2026, 10, 1, tzinfo=UTC)
    Task.objects.bulk_create(
        [
            Task(created_by=owner, title=f"dated {i}", status="todo", due_at=base + datetime.timedelta(days=i))
            for i in range(15)
        ]
    )
    Task.objects.create(created_by=owner, title="finished")
    _as(client, owner)

    with CaptureQueriesContext(connection) as captured:
        body = client.get("/tasks/").content.decode()

    due_soon = [q for q in captured.captured_queries if "due_at" in q["sql"] and "COUNT" not in q["sql"]]
    assert len(due_soon) == 1
    assert "LIMIT 10" in due_soon[0]["sql"]
    assert "dated 14" not in body


def test_the_team_directory_lists_people_and_never_a_token_column(client, people):
    owner, _mate, _admin = people
    owner.refresh_token = b"secret-refresh-bytes"
    owner.access_token = b"secret-access-bytes"
    owner.save(update_fields=["refresh_token", "access_token"])
    _as(client, owner)

    with CaptureQueriesContext(connection) as captured:
        body = client.get("/team/").content.decode()

    # The sign-in lookup reads the caller's own row, tokens included, which row
    # 2 allows. The directory is the other query: no WHERE clause, and it names
    # exactly the four columns the page may know.
    listing = [
        q["sql"]
        for q in captured.captured_queries
        if 'ORDER BY "accounts_appuser"."display_name"' in q["sql"]
    ]
    assert len(listing) == 1, [q["sql"][:90] for q in captured.captured_queries]
    assert "refresh_token" not in listing[0]
    assert "access_token" not in listing[0]
    assert listing[0].count(",") == 3, listing[0]
    assert "owner@example.com" in body
    assert "Boss" in body
    assert "secret-refresh-bytes" not in body


def test_a_fifty_row_board_page_reads_a_bounded_number_of_columns(client, people):
    owner, _mate, _admin = people
    Task.objects.bulk_create(
        [Task(created_by=owner, assignee=owner, title=f"task {i}") for i in range(50)]
    )
    _as(client, owner)

    with CaptureQueriesContext(connection) as captured:
        response = client.get("/tasks/")

    assert response.status_code == 200
    # Two joins, not fifty: the page itself and the due-soon column beside it.
    # Neither may carry a colleague's token columns into the page.
    joined = [q["sql"] for q in captured.captured_queries if 'FROM "task"' in q["sql"] and "accounts_appuser" in q["sql"]]
    assert len(joined) == 2, f"a board page should join twice, got {len(joined)} queries"
    for sql in joined:
        assert "refresh_token" not in sql
        assert "access_token" not in sql
        assert "LIMIT 50" in sql or "LIMIT 10" in sql
    # Five in total, and the number is the point: the session read, the sign-in
    # lookup of the caller, the page count, the 50 rows, the 10 due-soon rows.
    # None of them scales with the number of tasks on the page.
    assert len(captured.captured_queries) == 5, [q["sql"][:70] for q in captured.captured_queries]


def test_an_anonymous_caller_is_sent_to_sign_in_from_the_board(client, db):
    assert client.get("/tasks/").status_code == 302
    assert client.get("/team/").status_code == 302


def test_an_unknown_scope_falls_back_to_mine(client, people):
    owner, mate, _admin = people
    Task.objects.create(created_by=owner, assignee=owner, title="mine")
    Task.objects.create(created_by=owner, assignee=mate, title="theirs")
    _as(client, owner)

    body = client.get("/tasks/?scope=nonsense").content.decode()

    assert "mine" in body
    assert "theirs" not in body
