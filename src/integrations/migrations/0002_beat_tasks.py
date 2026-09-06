"""Register Beat schedules for the integrations sweeps (reconcile every 15 min, prune daily)."""

import typing as t

from django.db import migrations

_RECONCILE = "Reconcile platform listing counts"
_PRUNE = "Prune platform webhook deliveries"


def create_periodic_tasks(apps: t.Any, schema_editor: t.Any) -> None:
    """Create the two beat rows; idempotent via update_or_create."""
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    every_15, _ = IntervalSchedule.objects.get_or_create(every=15, period="minutes")
    PeriodicTask.objects.update_or_create(
        name=_RECONCILE, defaults={"task": "integrations.reconcile_counts", "interval": every_15, "crontab": None, "enabled": True}
    )
    daily, _ = CrontabSchedule.objects.get_or_create(
        minute="50", hour="4", day_of_week="*", day_of_month="*", month_of_year="*", timezone="UTC"
    )
    PeriodicTask.objects.update_or_create(
        name=_PRUNE, defaults={"task": "integrations.prune_webhook_deliveries", "crontab": daily, "interval": None, "enabled": True}
    )


def delete_periodic_tasks(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the beat rows."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name__in=[_RECONCILE, _PRUNE]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0001_initial"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]
    operations = [migrations.RunPython(create_periodic_tasks, reverse_code=delete_periodic_tasks)]
