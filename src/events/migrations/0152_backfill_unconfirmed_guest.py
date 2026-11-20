"""Backfill ``Ticket.unconfirmed_guest`` for paid guest checkouts made before the flag existed (#1430).

Before 0151 a guest's ONLINE checkout issued PENDING tickets with no email proof, and nothing recorded that.
A ticket is such a checkout when its holder is still a guest account, it came from checkout (or predates
``sale_source``), and it sits on an ONLINE tier or carries a Payment row. Free and offline guest tickets were
email-confirmed, so they are left alone. Accounts that already converted to a registered one are untraceable
(``guest`` is no longer set) and keep the flag False, an accepted gap. Idempotent: re-running (for example
after a rolling deploy, whose old code still inserts rows without the flag) only sets it again.
"""

import typing as t

from django.db import migrations
from django.db.models import Exists, OuterRef, Q

if t.TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def backfill(apps: "Apps", schema_editor: "BaseDatabaseSchemaEditor") -> None:
    Ticket = apps.get_model("events", "Ticket")
    Payment = apps.get_model("events", "Payment")
    Ticket.objects.filter(
        Q(sale_source__isnull=True) | Q(sale_source="checkout"),
        Q(tier__payment_method="online") | Exists(Payment.objects.filter(ticket_id=OuterRef("pk"))),
        user__guest=True,
        unconfirmed_guest=False,
    ).update(unconfirmed_guest=True)


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0151_ticket_unconfirmed_guest"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
