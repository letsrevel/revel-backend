"""Register Beat schedule for monthly revenue & VAT report delivery (#552)."""

import typing as t

from django.db import migrations


def create_periodic_tasks(apps: t.Any, schema_editor: t.Any) -> None:
    """Create the monthly revenue report delivery periodic task."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # Day 5 (not day 1 like ``events.generate_monthly_invoices``) gives a short settle
    # window for late refunds/chargebacks while still landing the snapshot well before a
    # 15th-of-month tax-declaration deadline — leaving the org's accountant ~10 days to
    # file. (Day 16 would arrive after such a deadline.) Quarterly orgs only actually send
    # in Jan/Apr/Jul/Oct (see ``closed_period_for``); the other months no-op.
    # UTC matches the sibling billing tasks (generate_monthly_invoices, revalidate_vat_ids);
    # the per-period boundaries are computed in each org's own timezone inside the task, so
    # the beat-level timezone only controls when the sweep fires, not the reporting period.
    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="6",
        day_of_week="*",
        day_of_month="5",
        month_of_year="*",
        timezone="UTC",
    )
    PeriodicTask.objects.update_or_create(
        name="Send scheduled revenue reports",
        defaults={
            "task": "events.send_scheduled_revenue_reports",
            "crontab": schedule,
            "enabled": True,
        },
    )


def delete_periodic_tasks(apps: t.Any, schema_editor: t.Any) -> None:
    """Remove the revenue report delivery periodic task."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Send scheduled revenue reports").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0084_organization_last_revenue_report_sent_period_and_more"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_periodic_tasks, reverse_code=delete_periodic_tasks),
    ]
