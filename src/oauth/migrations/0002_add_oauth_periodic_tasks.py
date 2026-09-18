"""Beat rows for the two OAuth sweeps (spec §10).

A data migration, so it is exempt from the one-schema-migration-per-app rule and must survive
a regeneration of ``0001_initial``. ``django_celery_beat.PeriodicTask`` references a task by its
registered *name* string, which is why every ``@shared_task`` in this app pins ``name=``.

Both run daily in the small hours, ten minutes apart so the prune sees a table ``cleartokens``
has already tidied: an app whose only credential was an expired access token is then genuinely
unused rather than looking used because of a dead row.
"""

import typing as t

from django.db import migrations

TASKS: tuple[tuple[str, str, str], ...] = (
    ("OAuth: clear expired tokens", "oauth.clear_expired_tokens", "10"),
    ("OAuth: prune unused dynamic clients", "oauth.prune_unused_dynamic_clients", "20"),
)


def create_oauth_periodic_tasks(apps: t.Any, schema_editor: t.Any) -> None:
    """Create the two daily periodic tasks."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    for name, task, minute in TASKS:
        schedule, _ = CrontabSchedule.objects.get_or_create(
            minute=minute,
            hour="4",
            day_of_week="*",
            day_of_month="*",
            month_of_year="*",
            timezone="UTC",
        )
        PeriodicTask.objects.update_or_create(
            name=name,
            defaults={"task": task, "crontab": schedule, "enabled": True},
        )


def delete_oauth_periodic_tasks(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the two periodic tasks, leaving their schedules for other tasks to share."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name__in=[name for name, _task, _minute in TASKS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("oauth", "0001_initial"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_oauth_periodic_tasks, reverse_code=delete_oauth_periodic_tasks),
    ]
