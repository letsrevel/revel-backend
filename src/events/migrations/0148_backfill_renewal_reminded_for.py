"""Backfill ``renewal_reminded_for`` so the widened reminder window doesn't re-remind on deploy.

Before 0147 the beat reminded each subscription exactly ``REMINDER_DAYS`` days before its period end,
without recording it. The new window also picks up periods ending sooner, so every active subscription
whose period ends between now and the target date was already reminded under the old rule: mark it.
Only a period that already existed on its reminder day (it started at least ``REMINDER_DAYS`` days before it
ends) could have been reminded; a newer, shorter one never was and keeps its one reminder. When the old beat
has already run today, the cohort that ends exactly on the target date was reminded too, so it is marked as well.
"""

import datetime
import typing as t

from django.db import migrations
from django.db.models import F
from django.utils import timezone

if t.TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor

REMINDER_DAYS = 3  # frozen copy of events.utils.subscription_periods.REMINDER_DAYS at the time of writing
REMINDER_TASK_NAME = "Send membership subscription renewal reminders"  # frozen copy from 0102


def _old_beat_ran_today(apps: "Apps") -> bool:
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    last_run_at = PeriodicTask.objects.filter(name=REMINDER_TASK_NAME).values_list("last_run_at", flat=True).first()
    return last_run_at is not None and timezone.localdate(last_run_at) == timezone.localdate()


def backfill(apps: "Apps", schema_editor: "BaseDatabaseSchemaEditor") -> None:
    MembershipSubscription = apps.get_model("events", "MembershipSubscription")
    target_date = timezone.localdate() + datetime.timedelta(days=REMINDER_DAYS)
    end_lookup = "current_period_end__date__lte" if _old_beat_ran_today(apps) else "current_period_end__date__lt"
    MembershipSubscription.objects.filter(
        status="active",
        cancel_at_period_end=False,
        renewal_reminded_for__isnull=True,
        current_period_end__gt=timezone.now(),
        current_period_start__lte=F("current_period_end") - datetime.timedelta(days=REMINDER_DAYS),
        **{end_lookup: target_date},
    ).update(renewal_reminded_for=F("current_period_end"))


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0147_renewal_reminded_for"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
