from django.urls import path

from accounts import views as auth_views
from mail import views as mail_views
from opshub import views
from tasks import views as task_views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("", views.home, name="home"),
    path("auth/google", auth_views.google_start, name="auth_google_start"),
    path("auth/callback", auth_views.google_callback, name="auth_google_callback"),
    path("reconnect", auth_views.reconnect, name="auth_reconnect"),
    path("team/", task_views.team, name="team_directory"),
    # mail routes are all str-converters, not int: a Gmail thread id is an
    # opaque base64-ish token, and there is no numeric id to expose here.
    path("mail/inbox", mail_views.inbox, name="mail_inbox"),
    path("mail/thread/<str:thread_id>", mail_views.thread, name="mail_thread"),
    path("mail/thread/<str:thread_id>/reply", mail_views.reply, name="mail_reply"),
    # "new" is a literal segment ahead of the int converter, so a task can
    # never be created at an id-shaped address and a request for a task named
    # "new" is not a task request.
    path("tasks/new", task_views.create, name="task_create"),
    path("tasks/", task_views.board, name="task_board"),
    path("tasks/<int:task_id>", task_views.detail, name="task_detail"),
    path("tasks/<int:task_id>/edit", task_views.edit, name="task_edit"),
    path("tasks/<int:task_id>/claim", task_views.claim, name="task_claim"),
    path("tasks/<int:task_id>/delete", task_views.delete, name="task_delete"),
]
