"""Backfill ``renewal_reminded_for`` so the widened reminder window doesn't re-remind on deploy.

Before 0147 the beat reminded each subscription exactly ``REMINDER_DAYS`` days before its period end,
without recording it. The new window also picks up periods ending sooner, so every active subscription
whose period ends between now and the target date was already reminded under the old rule: mark it.
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
        current_period_end__date__lt=target_date,
    ).update(renewal_reminded_for=F("current_period_end"))


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0147_renewal_reminded_for"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
