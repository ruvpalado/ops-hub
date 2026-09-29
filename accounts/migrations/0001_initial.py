# Placeholder boot migration: exists so the DB schema runs first before any
# model exists yet. App models arrive in 0002 (AppUser) and 0003 (Source).
from django.db import migrations


class Migration(migrations.Migration):
    """00{2,3} build the schema; this one only bootstraps the app.

    Keeping an initial migration here means a brand-new environment can apply
    the full migration chain in one `migrate` call, exactly like production.
    """

    initial = True

    dependencies = []

    operations = []
