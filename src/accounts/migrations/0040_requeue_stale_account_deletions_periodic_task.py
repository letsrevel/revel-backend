import typing as t

from django.db import migrations

_TASK_NAME = "Requeue stale account deletions"


def create_requeue_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Register the 15-minute sweep that queues a stale self-service account deletion again (#1476)."""
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    every_15_minutes, _ = IntervalSchedule.objects.get_or_create(every=15, period="minutes")

    # get_or_create, not update_or_create: re-applying must never re-enable a task an admin switched off.
    PeriodicTask.objects.get_or_create(
        name=_TASK_NAME,
        defaults={
            "task": "accounts.requeue_stale_account_deletions",
            "interval": every_15_minutes,
            "enabled": True,
        },
    )


def delete_requeue_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the stale-deletion requeue beat task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=_TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0039_erased_account_holder_snapshot"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_requeue_task, reverse_code=delete_requeue_task),
    ]
