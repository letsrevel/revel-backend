"""Preserve pre-layered ``max_tickets_per_user`` allowances exactly.

Old semantics: a tier with ``max_tickets_per_user = NULL`` *inherited* the event's
value as its own per-tier cap, and a tier that set a value *replaced* the event's.
New semantics (this branch): the event cap is a cross-tier total, the tier cap is an
independent per-tier ceiling, and a NULL tier cap means "no per-tier cap".

Because ``Event.max_tickets_per_user`` has ``default=1``, doing nothing would silently
shrink every existing event that relied on a tier override (``min(tier_cap, 1) == 1``).
So for every event that has at least one tier and a non-NULL cap we materialize the old
inheritance onto its NULL tiers and then clear the event cap, which reproduces the old
effective allowance byte-for-byte under the new engine.

Events with a cap but no tiers are left alone: there is no inheritance to preserve, and
their cap simply becomes the cross-tier total — the correct reading for an unconfigured
event.

The reverse is a deliberate no-op: it cannot distinguish a materialized cap from one the
organizer typed, so undoing it would be lossy. It is also unnecessary — this migration
preserves behavior, and old code treats a materialized tier cap exactly like an inherited
one, so rolling the code back without reversing the data is safe.
"""

import typing as t

from django.db import migrations
from django.db.models import Exists, OuterRef, Subquery


def backfill_layered_caps(apps: t.Any, schema_editor: t.Any) -> None:
    Event = apps.get_model("events", "Event")
    TicketTier = apps.get_model("events", "TicketTier")

    # 1. Materialize the old inheritance: NULL tiers of a capped event take the event's value.
    TicketTier.objects.filter(
        max_tickets_per_user__isnull=True,
        event__max_tickets_per_user__isnull=False,
    ).update(
        max_tickets_per_user=Subquery(
            Event.objects.filter(pk=OuterRef("event_id")).values("max_tickets_per_user")[:1]
        )
    )

    # 2. Clear the event cap so it no longer applies as a cross-tier total on top of the
    #    per-tier caps we just wrote (which alone reproduce the old allowances).
    Event.objects.filter(
        max_tickets_per_user__isnull=False,
    ).filter(
        Exists(TicketTier.objects.filter(event_id=OuterRef("pk"))),
    ).update(max_tickets_per_user=None)


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0115_alter_event_max_tickets_per_user_and_more"),
    ]
    operations = [
        migrations.RunPython(backfill_layered_caps, migrations.RunPython.noop),
    ]
