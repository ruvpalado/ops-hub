from django.urls import path

from accounts import views as auth_views
from opshub import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("auth/google", auth_views.google_start, name="auth_google_start"),
]