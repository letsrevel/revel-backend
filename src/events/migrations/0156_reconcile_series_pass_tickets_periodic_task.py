import typing as t

from django.db import migrations

_TASK_NAME = "Reconcile series pass holder tickets"


def create_reconcile_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Register the 15-minute sweep that issues the covered-event tickets a failed dispatch missed (#1536)."""
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    every_15_minutes, _ = IntervalSchedule.objects.get_or_create(every=15, period="minutes")

    # get_or_create, not update_or_create: re-applying must never re-enable a task an admin switched off.
    PeriodicTask.objects.get_or_create(
        name=_TASK_NAME,
        defaults={
            "task": "events.reconcile_series_pass_tickets",
            "interval": every_15_minutes,
            "enabled": True,
        },
    )


def delete_reconcile_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the series pass reconcile beat task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=_TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0155_erased_account_holder_snapshot"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_reconcile_task, reverse_code=delete_reconcile_task),
    ]
