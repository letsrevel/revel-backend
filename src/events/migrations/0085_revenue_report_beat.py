"""Register Beat schedule for monthly revenue & VAT report delivery (#552)."""

import typing as t

from django.db import migrations


def create_periodic_tasks(apps: t.Any, schema_editor: t.Any) -> None:
    """Create the monthly revenue report delivery periodic task."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="6",
        day_of_week="*",
        day_of_month="16",
        month_of_year="*",
        timezone="Europe/Vienna",
    )
    PeriodicTask.objects.update_or_create(
        name="Send scheduled revenue reports",
        defaults={
            "task": "events.send_scheduled_revenue_reports",
            "crontab": schedule,
            "enabled": True,
        },
    )


def delete_periodic_tasks(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the revenue report delivery periodic task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Send scheduled revenue reports").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0084_organization_last_revenue_report_sent_period_and_more"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_periodic_tasks, reverse_code=delete_periodic_tasks),
    ]
