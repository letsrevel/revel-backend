"""Backfill ``Ticket.unconfirmed_guest`` for paid guest checkouts made before the flag existed (#1430).

Before 0151 a guest's ONLINE checkout issued PENDING tickets with no email proof, and nothing recorded that.
A ticket is such a checkout when its holder is still a guest account, it came from checkout (or predates
``sale_source``), and it sits on an ONLINE tier or carries a Payment row. Free and offline guest tickets were
email-confirmed, so they are left alone. Accounts that already converted to a registered one are untraceable
(``guest`` is no longer set) and keep the flag False, an accepted gap. The UPDATE lives in
``events.utils.unconfirmed_guest``. A rolling deploy's old code still inserts rows without the flag, so ops run
``manage.py backfill_unconfirmed_guest --before <rollout-finished time>`` ONCE after the rollout (dry-run first).
Never re-run it later: email-confirmed zero-total guest tickets created after the deploy match the same shape.
"""

import typing as t

from django.db import migrations

from events.utils.unconfirmed_guest import backfill_unconfirmed_guest

if t.TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def backfill(apps: "Apps", schema_editor: "BaseDatabaseSchemaEditor") -> None:
    backfill_unconfirmed_guest(apps.get_model("events", "Ticket"), apps.get_model("events", "Payment"))


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0151_ticket_unconfirmed_guest"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
