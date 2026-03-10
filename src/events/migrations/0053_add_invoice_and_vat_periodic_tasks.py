# Data migration: register Celery Beat periodic tasks for invoice generation and VAT re-validation.

from django.db import migrations


def create_periodic_tasks(apps, schema_editor):
    """Register monthly invoice generation and VAT re-validation periodic tasks."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # 1st of each month at 02:00 UTC
    first_of_month, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="2",
        day_of_week="*",
        day_of_month="1",
        month_of_year="*",
        timezone="UTC",
    )

    PeriodicTask.objects.update_or_create(
        name="Generate monthly platform fee invoices",
        defaults={
            "task": "events.generate_monthly_invoices",
            "crontab": first_of_month,
            "enabled": True,
        },
    )

    # 15th of each month at 04:00 UTC
    fifteenth_of_month, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="4",
        day_of_week="*",
        day_of_month="15",
        month_of_year="*",
        timezone="UTC",
    )

    PeriodicTask.objects.update_or_create(
        name="Re-validate organization VAT IDs via VIES",
        defaults={
            "task": "events.revalidate_vat_ids",
            "crontab": fifteenth_of_month,
            "enabled": True,
        },
    )


def delete_periodic_tasks(apps, schema_editor):
    """Remove the periodic tasks."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    PeriodicTask.objects.filter(
        name__in=[
            "Generate monthly platform fee invoices",
            "Re-validate organization VAT IDs via VIES",
        ]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0052_organization_billing_address_and_more"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(
            create_periodic_tasks,
            reverse_code=delete_periodic_tasks,
        ),
    ]
