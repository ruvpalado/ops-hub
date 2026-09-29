"""Audit trail: append-only at the database, scrubbed at the writer."""

import pytest
from django.db import DatabaseError, connection, transaction

from accounts.models import AppUser
from audit import writer
from audit.models import AuditLog


def test_audit_log_rejects_update_and_delete(db):
    u = AppUser.objects.create(email="audit@example.com", display_name="A")
    entry = writer.record(actor=u, action="task.create", target_type="task", target_id=1)
    assert entry is not None
    with pytest.raises(DatabaseError):
        with transaction.atomic():
            AuditLog.objects.filter(pk=entry.pk).update(action="tampered")
    with pytest.raises(DatabaseError):
        with transaction.atomic():
            AuditLog.objects.filter(pk=entry.pk).delete()
    entry.refresh_from_db()
    assert entry.action == "task.create"
    assert AuditLog.objects.filter(pk=entry.pk).exists()


def test_audit_writer_drops_secret_metadata(db):
    u = AppUser.objects.create(email="scrub@example.com", display_name="S")
    entry = writer.record(
        actor=u,
        action="auth.token_stored",
        target_type="user",
        target_id=u.pk,
        metadata={"kind": "gmail", "access_token": "ya29.LEAK", "refresh_token": "1//LEAK", "code": "4/LEAK"},
    )
    assert entry.metadata == {"kind": "gmail"}
    with connection.cursor() as cursor:
        cursor.execute("SELECT metadata::text FROM audit_auditlog WHERE id = %s", [entry.pk])
        stored = cursor.fetchone()[0]
    assert "LEAK" not in stored