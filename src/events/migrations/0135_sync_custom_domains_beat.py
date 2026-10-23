import typing as t

from django.db import migrations

_TASK_NAME = "Sync pending custom domains from Cloudflare"


def create_sync_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Register the 15-minute custom-domain status sync beat task (#1151)."""
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    every_15_minutes, _ = IntervalSchedule.objects.get_or_create(every=15, period="minutes")

    PeriodicTask.objects.update_or_create(
        name=_TASK_NAME,
        defaults={
            "task": "events.sync_custom_domains",
            "interval": every_15_minutes,
            "enabled": True,
        },
    )


def delete_sync_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the custom-domain status sync beat task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=_TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0134_organizationdomain_last_errors"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_sync_task, reverse_code=delete_sync_task),
    ]
