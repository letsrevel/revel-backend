import typing as t

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def create_close_polls_periodic_task(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    IntervalSchedule: t.Any = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask: t.Any = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = IntervalSchedule.objects.get_or_create(every=1, period="minutes")
    PeriodicTask.objects.update_or_create(
        name="Close polls due",
        defaults={
            "task": "polls.tasks.close_polls_due",
            "interval": schedule,
            "enabled": True,
        },
    )


def delete_close_polls_periodic_task(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    PeriodicTask: t.Any = apps.get_model("django_celery_beat", "PeriodicTask")
    # ``filter(...).delete()`` is idempotent and avoids swallowing unrelated
    # DoesNotExist errors that would have hidden bugs in the old try/except.
    PeriodicTask.objects.filter(name="Close polls due").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("polls", "0001_initial"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_close_polls_periodic_task, reverse_code=delete_close_polls_periodic_task),
    ]
