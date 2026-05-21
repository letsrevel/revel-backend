"""Register hourly Beat schedule for nudge_open_waitlists_task."""

import typing as t

from django.db import migrations


def create_periodic_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Create periodic task to nudge open waitlists hourly."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # Run every hour at minute 30 UTC — offset from the expire-sweeper at
    # minute 0 so the two tasks don't compete on locks at the same instant.
    hourly_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="30",
        hour="*",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="UTC",
    )

    PeriodicTask.objects.update_or_create(
        name="Nudge open waitlists",
        defaults={
            "task": "events.nudge_open_waitlists",
            "crontab": hourly_schedule,
            "enabled": True,
        },
    )


def delete_periodic_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the nudge-waitlists periodic task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Nudge open waitlists").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0074_waitlist_periodic_task"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(
            create_periodic_task,
            reverse_code=delete_periodic_task,
        ),
    ]
