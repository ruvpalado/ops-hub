"""Per-user, per-source sync state. One row per (user, kind) — the unique
constraint is what makes the sync commands idempotent: upsert by that pair
instead of racing to insert duplicates.

The status and last_error_code vocabularies are enforced by the database, not
by convention. A typo in a sync handler then fails loudly at write time rather
than silently creating a status the scheduler never picks up.
"""

from __future__ import annotations

from django.db import models

from accounts.models import AppUser


def _check(values: tuple[str, ...]) -> str:
    return "IN (" + ", ".join(f"'{v}'" for v in values) + ")"


SOURCE_KINDS = ("gmail", "calendar")
SOURCE_STATUSES = ("active", "needs_reauth", "error", "disconnected")
SOURCE_ERROR_CODES = (
    "invalid_grant",
    "no_token",
    "insufficient_scope",
    "sync_token_expired",
    "rate_limited",
    "upstream_5xx",
    "network",
)


class Source(models.Model):
    class Kind(models.TextChoices):
        GMAIL = "gmail"
        CALENDAR = "calendar"

    class Status(models.TextChoices):
        ACTIVE = "active"
        NEEDS_REAUTH = "needs_reauth"
        ERROR = "error"
        DISCONNECTED = "disconnected"

    user = models.ForeignKey(AppUser, on_delete=models.CASCADE, related_name="sources")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)

    # cursor: gmail historyId or calendar syncToken, as text; opaque on purpose
    cursor = models.TextField(null=True, blank=True)
    last_error_code = models.CharField(max_length=32, null=True, blank=True)

    # next_attempt_at gates the scheduler's polling loop; NULL means "not scheduled"
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "kind"], name="source_user_kind_unique"),
            models.CheckConstraint(
                condition=models.Q(kind__in=SOURCE_KINDS),
                name="source_kind_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=SOURCE_STATUSES),
                name="source_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(last_error_code__isnull=True)
                | models.Q(last_error_code__in=SOURCE_ERROR_CODES),
                name="source_error_code_valid",
            ),
        ]
        indexes = [
            # the scheduler's real query: active-ish rows due for a retry
            models.Index(fields=["status", "next_attempt_at"], name="source_status_next_attempt_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.kind}:{self.status}"