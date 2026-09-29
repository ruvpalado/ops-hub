"""Append-only audit trail. The database, not the application, is what makes
it append-only: triggers on UPDATE and DELETE raise, so even a careless
raw-SQL script cannot rewrite history. Writes still go through the ORM; nobody
needs UPDATE here.
"""

from __future__ import annotations

from django.db import models

from accounts.models import AppUser


class AuditLog(models.Model):
    actor = models.ForeignKey(
        AppUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_entries",
    )
    # actor is null for system-driven actions (the scheduler, a failed refresh)
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=64)
    target_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["created_at"], name="audit_log_created_idx")]

    def __str__(self) -> str:
        return f"{self.action} {self.target_type}:{self.target_id}"