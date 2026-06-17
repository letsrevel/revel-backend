"""Register Beat schedules for the announcement sweeps."""

import typing as t

from django.db import migrations


def create_periodic_tasks(apps: t.Any, schema_editor: t.Any) -> None:
    """Create the scheduled-send (5 min) and resend (15 min) periodic tasks."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    every_5_min, _ = CrontabSchedule.objects.get_or_create(
        minute="*/5", hour="*", day_of_week="*", day_of_month="*", month_of_year="*", timezone="UTC",
    )
    # Offset off the 5-min boundaries so the heavier resend sweep doesn't compete
    # for locks with the scheduled-send sweep.
    every_15_min, _ = CrontabSchedule.objects.get_or_create(
        minute="7,22,37,52", hour="*", day_of_week="*", day_of_month="*", month_of_year="*", timezone="UTC",
    )

    PeriodicTask.objects.update_or_create(
        name="Send scheduled announcements",
        defaults={"task": "events.send_scheduled_announcements", "crontab": every_5_min, "enabled": True},
    )
    PeriodicTask.objects.update_or_create(
        name="Resend announcements to new sign-ups",
        defaults={"task": "events.resend_announcements_to_new_signups", "crontab": every_15_min, "enabled": True},
    )


def delete_periodic_tasks(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the announcement sweep periodic tasks."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(
        name__in=["Send scheduled announcements", "Resend announcements to new sign-ups"]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0081_announcement_scheduling"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_periodic_tasks, reverse_code=delete_periodic_tasks),
    ]
