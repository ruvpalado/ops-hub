"""Make audit_log append-only at the database level.

An application-level "never update the audit log" is a convention. A trigger
that raises is a rule: UPDATE and DELETE are refused by Postgres itself, no
matter which code path, ORM or raw SQL, attempts them. The only escape hatch
is a superuser disabling the trigger, which is visible in the migration log.
"""

from django.db import migrations

TRIGGER_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only; % is not permitted', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

TRIGGERS = """
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_auditlog
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_auditlog
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
"""

DROP = """
DROP TRIGGER IF EXISTS audit_log_no_update ON audit_auditlog;
DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_auditlog;
DROP FUNCTION IF EXISTS audit_log_append_only();
"""


def create_triggers(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(TRIGGER_FUNCTION)
        cursor.execute(TRIGGERS)


def drop_triggers(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(DROP)


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]

    operations = [migrations.RunPython(create_triggers, drop_triggers)]