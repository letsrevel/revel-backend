from django.db import migrations

_RECONCILE_TASK_NAME = "Reconcile Stripe membership subscriptions"


def create_subscription_reconcile_task(apps, schema_editor):
    """Register the nightly Stripe-subscription reconciliation beat task."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # Daily at 03:30 UTC — before grace-expiry finalisation at 04:00 UTC, so
    # the expiry task acts on freshly-mirrored Stripe state.
    daily_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="30",
        hour="3",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="UTC",
    )

    PeriodicTask.objects.update_or_create(
        name=_RECONCILE_TASK_NAME,
        defaults={
            "task": "events.reconcile_stripe_subscriptions",
            "crontab": daily_schedule,
            "enabled": True,
        },
    )


def delete_subscription_reconcile_task(apps, schema_editor):
    """Remove the Stripe-subscription reconciliation beat task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=_RECONCILE_TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0081_subscription_renewal_reminder_beat"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(
            create_subscription_reconcile_task,
            reverse_code=delete_subscription_reconcile_task,
        ),
    ]
