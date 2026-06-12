from django.db import migrations


_REMINDER_TASK_NAME = "Send membership subscription renewal reminders"


def create_subscription_renewal_reminder_task(apps, schema_editor):
    """Register the daily subscription-renewal-reminder beat task."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # Daily at 05:00 UTC (one hour after expiry finalisation at 04:00 UTC).
    daily_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="5",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="UTC",
    )

    PeriodicTask.objects.update_or_create(
        name=_REMINDER_TASK_NAME,
        defaults={
            "task": "events.send_subscription_renewal_reminders",
            "crontab": daily_schedule,
            "enabled": True,
        },
    )


def delete_subscription_renewal_reminder_task(apps, schema_editor):
    """Remove the subscription-renewal-reminder beat task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=_REMINDER_TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0080_membershippayment_stripe_invoice_id_and_more"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(
            create_subscription_renewal_reminder_task,
            reverse_code=delete_subscription_renewal_reminder_task,
        ),
    ]
