"""Register daily Beat schedule for prune_stripe_webhook_events."""

import typing as t

from django.db import migrations


def create_periodic_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Create periodic task to prune the Stripe webhook event log daily."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="45",
        hour="4",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="UTC",
    )

    PeriodicTask.objects.update_or_create(
        name="Prune Stripe webhook events",
        defaults={
            "task": "events.prune_stripe_webhook_events",
            "crontab": schedule,
            "enabled": True,
        },
    )


def delete_periodic_task(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the pruning periodic task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Prune Stripe webhook events").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0078_stripewebhookevent"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(
            create_periodic_task,
            reverse_code=delete_periodic_task,
        ),
    ]
