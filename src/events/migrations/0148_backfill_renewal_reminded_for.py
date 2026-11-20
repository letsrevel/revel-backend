"""Backfill ``renewal_reminded_for`` so the widened reminder window doesn't re-remind on deploy.

Before 0147 the beat reminded each subscription exactly ``REMINDER_DAYS`` days before its period end,
without recording it. The new window also picks up periods ending sooner, so every active subscription
whose period ends between now and the target date was already reminded under the old rule: mark it.
Only a period that already existed on its reminder day (it started at least ``REMINDER_DAYS`` days before it
ends) could have been reminded; a newer, shorter one never was and keeps its one reminder.
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


def backfill(apps: "Apps", schema_editor: "BaseDatabaseSchemaEditor") -> None:
    MembershipSubscription = apps.get_model("events", "MembershipSubscription")
    target_date = timezone.localdate() + datetime.timedelta(days=REMINDER_DAYS)
    MembershipSubscription.objects.filter(
        status="active",
        cancel_at_period_end=False,
        renewal_reminded_for__isnull=True,
        current_period_end__gt=timezone.now(),
        # ponytail: the cohort ending exactly on target_date is left for the beat. If this migration runs after
        # the old 05:00 UTC run that day, that cohort gets one extra reminder; a duplicate is safer than a
        # missed reminder. Deploy before 05:00 UTC to avoid it.
        current_period_end__date__lt=target_date,
        current_period_start__lte=F("current_period_end") - datetime.timedelta(days=REMINDER_DAYS),
    ).update(renewal_reminded_for=F("current_period_end"))


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0147_renewal_reminded_for"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
