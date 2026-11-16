import typing as t

from django.db import migrations

_TASK_NAME = "Send daily admin recap"


def create_recap_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Register the daily 09:00 Europe/Vienna admin recap beat task (#1390)."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="9",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="Europe/Vienna",
    )
    PeriodicTask.objects.update_or_create(
        name=_TASK_NAME,
        defaults={"task": "events.tasks.send_daily_admin_recap", "crontab": schedule, "enabled": True},
    )


def delete_recap_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the daily admin recap beat task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=_TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0142_alter_historicalmembershippayment_status_and_more"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_recap_task, reverse_code=delete_recap_task),
    ]
