"""Backfill ``renewal_reminded_for`` so the widened reminder window doesn't re-remind on deploy.

Before 0147 the beat reminded each subscription exactly ``REMINDER_DAYS`` days before its period end,
without recording it. The new window also picks up periods ending sooner, so every active subscription
whose period ends between now and the target date was already reminded under the old rule: mark it.
Only a period that already existed on its reminder day (it started at least ``REMINDER_DAYS`` days before it
ends) could have been reminded; a newer, shorter one never was and keeps its one reminder. When the old beat
has already run today, the cohort that ends exactly on the target date and existed at that run was reminded too, so it is marked as well.
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


REMINDER_RUN_TIME_UTC = datetime.time(5, 0)  # frozen copy of the 0102 crontab (daily 05:00 UTC)


def _old_run_moment_today(apps: "Apps") -> datetime.datetime | None:
    """When the old daily run reminded today's cohort, or None if it has not run today.

    ``last_run_at`` is synced to the database only every few minutes, so the scheduled time itself also counts.
    """
    now = timezone.now()
    scheduled = datetime.datetime.combine(now.astimezone(datetime.UTC).date(), REMINDER_RUN_TIME_UTC, datetime.UTC)
    if now >= scheduled:
        return scheduled
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    last_run_at = PeriodicTask.objects.filter(name=REMINDER_TASK_NAME).values_list("last_run_at", flat=True).first()
    if last_run_at is not None and timezone.localdate(last_run_at) == timezone.localdate():
        return t.cast(datetime.datetime, last_run_at)
    return None


def backfill(apps: "Apps", schema_editor: "BaseDatabaseSchemaEditor") -> None:
    MembershipSubscription = apps.get_model("events", "MembershipSubscription")
    target_date = timezone.localdate() + datetime.timedelta(days=REMINDER_DAYS)
    reminded = MembershipSubscription.objects.filter(
        status="active",
        cancel_at_period_end=False,
        renewal_reminded_for__isnull=True,
        current_period_end__gt=timezone.now(),
        current_period_start__lte=F("current_period_end") - datetime.timedelta(days=REMINDER_DAYS),
    )
    reminded.filter(current_period_end__date__lt=target_date).update(renewal_reminded_for=F("current_period_end"))
    run_moment = _old_run_moment_today(apps)
    if run_moment is not None:
        # Only periods that already existed at that run received its reminder.
        reminded.filter(current_period_end__date=target_date, current_period_start__lt=run_moment).update(
            renewal_reminded_for=F("current_period_end")
        )


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0147_renewal_reminded_for"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
