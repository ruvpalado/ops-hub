from django.urls import path

from opshub import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
]