"""The application user. Not Django's auth user: roles live here, sessions are
keyed on this row, and nothing about this model pretends to be a superuser.

Enforcement happens at the schema, not in the app: one active email per user,
and at most one row per linked Google subject. The google_sub partial unique
index is the load-bearing constraint — two people linking the same Google
account must not end up as two identities with two token sets.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q


class AppUser(models.Model):
    class Role(models.TextChoices):
        ADMIN = "admin"
        LEAD = "lead"
        MEMBER = "member"

    google_sub = models.CharField(max_length=64, null=True, blank=True)
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=120)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    timezone = models.CharField(max_length=64, default="UTC", blank=True)
    refresh_token_obtained_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["google_sub"],
                name="app_user_google_sub_unique_nonnull",
                condition=Q(google_sub__isnull=False),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.email} ({self.role})"