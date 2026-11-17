import typing as t

from django.db import migrations

_SWEEP_TASK_NAME = "Expire unpaid offline subscriptions"


def create_unpaid_offline_sweep_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Register the daily never-paid offline subscription expiry beat task."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # Daily at 04:45 UTC, after the 04:30 stale-application sweep.
    daily_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="45",
        hour="4",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="UTC",
    )

    PeriodicTask.objects.update_or_create(
        name=_SWEEP_TASK_NAME,
        defaults={
            "task": "events.expire_unpaid_offline_subscriptions",
            "crontab": daily_schedule,
            "enabled": True,
        },
    )


def delete_unpaid_offline_sweep_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the never-paid offline subscription expiry beat task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=_SWEEP_TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0145_offline_payment_instructions"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(
            create_unpaid_offline_sweep_task,
            reverse_code=delete_unpaid_offline_sweep_task,
        ),
    ]
