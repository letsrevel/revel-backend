import typing as t

from django.db import migrations

_TASK_NAME = "Notify organizers of skipped fiscal documents"


def create_digest_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Register the daily skipped-fiscal-documents digest beat task (#1073)."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # Daily at 07:00 UTC: morning in Europe, when the organizer can act on it.
    daily_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="7",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="UTC",
    )

    PeriodicTask.objects.update_or_create(
        name=_TASK_NAME,
        defaults={
            "task": "events.notify_skipped_fiscal_documents",
            "crontab": daily_schedule,
            "enabled": True,
        },
    )


def delete_digest_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the skipped-fiscal-documents digest beat task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=_TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0130_skipped_fiscal_document"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_digest_task, reverse_code=delete_digest_task),
    ]
