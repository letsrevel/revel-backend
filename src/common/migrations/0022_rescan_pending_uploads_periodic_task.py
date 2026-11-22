import typing as t

from django.db import migrations

_TASK_NAME = "Rescan uploads pending a malware scan"


def create_rescan_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Register the 15-minute sweep that queues pending malware scans again (#1446)."""
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    every_15_minutes, _ = IntervalSchedule.objects.get_or_create(every=15, period="minutes")

    # get_or_create, not update_or_create: re-applying must never re-enable a task an admin switched off.
    PeriodicTask.objects.get_or_create(
        name=_TASK_NAME,
        defaults={
            "task": "common.tasks.rescan_pending_uploads",
            "interval": every_15_minutes,
            "enabled": True,
        },
    )


def delete_rescan_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the pending-upload rescan beat task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=_TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0021_sitesettings_notify_guest_user_joined"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_rescan_task, reverse_code=delete_rescan_task),
    ]
