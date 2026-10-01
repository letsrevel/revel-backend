"""Register the daily Beat schedule for send_org_nudges — DISABLED.

Ships switched off: preview who would be nudged with ``manage.py org_nudges`` first,
then enable "Send org setup nudges" in Django admin → Periodic tasks.
"""

import typing as t

from django.db import migrations

TASK_NAME = "Send org setup nudges"


def create_periodic_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Create the (disabled) daily periodic task for org setup nudges."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # 10:00 Vienna: lands in a European working morning, never at night.
    daily_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="10",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="Europe/Vienna",
    )

    # get_or_create, not update_or_create: re-applying must never flip off a task an
    # admin has switched on.
    PeriodicTask.objects.get_or_create(
        name=TASK_NAME,
        defaults={
            "task": "events.send_org_nudges",
            "crontab": daily_schedule,
            "enabled": False,
        },
    )


def delete_periodic_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the org setup nudges periodic task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0124_org_setup_nudges"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(
            create_periodic_task,
            reverse_code=delete_periodic_task,
        ),
    ]
