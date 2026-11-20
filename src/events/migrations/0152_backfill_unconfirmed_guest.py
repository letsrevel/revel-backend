"""Backfill ``Ticket.unconfirmed_guest`` for paid guest checkouts made before the flag existed (#1430).

Before 0151 a guest's ONLINE checkout issued PENDING tickets with no email proof, and nothing recorded that.
A ticket is such a checkout when its holder is still a guest account, it came from checkout (or predates
``sale_source``), and it carries a Payment row, the only evidence of a paid checkout. Free, offline and
email-confirmed zero-total guest tickets have no Payment row, so they are left alone. Accepted gaps, which keep
the flag False: accounts that already converted to a registered one (``guest`` is no longer set), and ONLINE
guest tickets with no Payment row. One residual is flagged although confirmed: a guest who clicked the email
link after the tier became ONLINE was reserved with Payment rows (rare; it fails closed). The UPDATE lives in ``events.utils.unconfirmed_guest``. A rolling deploy's
old code still inserts rows without the flag, so ops run ``manage.py backfill_unconfirmed_guest --before
<moment the last old web container stopped>`` ONCE after the rollout (dry-run first).
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
