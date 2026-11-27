import json
import typing as t

from django.db import migrations

_TASK_NAME = "Sweep unreferenced image files"


def create_sweep_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Register the daily sweep of image files that no row names, as a dry run (#1548).

    Deletes stay off until an admin sets the kwarg ``dry_run`` to false, after a look at the dry-run counts.
    """
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    daily, _ = CrontabSchedule.objects.get_or_create(
        minute="10",
        hour="4",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="UTC",
    )

    # get_or_create, not update_or_create: re-applying must never re-enable a task, or turn deletes back off.
    PeriodicTask.objects.get_or_create(
        name=_TASK_NAME,
        defaults={
            "task": "common.sweep_unreferenced_image_files",
            "crontab": daily,
            "kwargs": json.dumps({"dry_run": True}),
            "enabled": True,
        },
    )


def delete_sweep_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the image-file sweep beat task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=_TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0026_file_upload_audit_file_path"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_sweep_task, reverse_code=delete_sweep_task),
    ]
