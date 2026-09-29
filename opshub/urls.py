from django.urls import path

from accounts import views as auth_views
from opshub import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("", views.home, name="home"),
    path("auth/google", auth_views.google_start, name="auth_google_start"),
    path("auth/callback", auth_views.google_callback, name="auth_google_callback"),
]