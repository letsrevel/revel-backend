"""Register hourly Beat schedule for expire_waitlist_offers_task."""

import typing as t

from django.db import migrations


def create_periodic_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Create periodic task to expire waitlist offers hourly."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # Run every hour at minute 0 UTC.
    hourly_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="*",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="UTC",
    )

    PeriodicTask.objects.update_or_create(
        name="Expire waitlist offers",
        defaults={
            "task": "events.expire_waitlist_offers",
            "crontab": hourly_schedule,
            "enabled": True,
        },
    )


def delete_periodic_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the expire-waitlist periodic task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Expire waitlist offers").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0074_waitlistoffer"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(
            create_periodic_task,
            reverse_code=delete_periodic_task,
        ),
    ]
