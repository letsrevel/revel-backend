import typing as t

from django.db import migrations

_TASK_NAME = "Sweep orphan data export files"


def create_sweep_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Register the daily sweep that deletes data-export files that no export row names (#1476)."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    daily, _ = CrontabSchedule.objects.get_or_create(
        minute="40",
        hour="3",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="UTC",
    )

    # get_or_create, not update_or_create: re-applying must never re-enable a task an admin switched off.
    PeriodicTask.objects.get_or_create(
        name=_TASK_NAME,
        defaults={
            "task": "accounts.sweep_orphan_data_exports",
            "crontab": daily,
            "enabled": True,
        },
    )


def delete_sweep_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the orphan data-export sweep beat task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=_TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0040_requeue_stale_account_deletions_periodic_task"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_sweep_task, reverse_code=delete_sweep_task),
    ]
