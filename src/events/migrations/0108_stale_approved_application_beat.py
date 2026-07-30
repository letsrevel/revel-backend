import typing as t

from django.db import migrations

_SWEEP_TASK_NAME = "Expire stale approved membership applications"


def create_stale_application_sweep_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Register the daily approved-but-unpaid application expiry beat task."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # Daily at 04:30 UTC — after grace-expiry finalisation at 04:00 UTC, so a
    # subscription terminalised that night frees its application the same run.
    daily_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="30",
        hour="4",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="UTC",
    )

    PeriodicTask.objects.update_or_create(
        name=_SWEEP_TASK_NAME,
        defaults={
            "task": "events.expire_stale_approved_applications",
            "crontab": daily_schedule,
            "enabled": True,
        },
    )


def delete_stale_application_sweep_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the approved-but-unpaid application expiry beat task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=_SWEEP_TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0107_alter_membershiptier_membership_questionnaire_and_more"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(
            create_stale_application_sweep_task,
            reverse_code=delete_stale_application_sweep_task,
        ),
    ]
