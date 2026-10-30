"""Intentionally a no-op (kept so environments that applied an earlier version stay consistent).

The legacy backfill for guests converted before #1248 is the ``reset_converted_guest_prefs``
management command, run by an operator after a dry-run.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0036_alter_notification_notification_type"),
    ]

    operations = [migrations.RunPython(migrations.RunPython.noop, migrations.RunPython.noop)]
