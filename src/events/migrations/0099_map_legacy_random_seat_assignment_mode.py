import typing as t

from django.db import migrations

if t.TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def convert_random_tiers(apps: "Apps", schema_editor: "BaseDatabaseSchemaEditor") -> None:
    """Map the removed RANDOM mode onto BEST_AVAILABLE.

    Databases migrating from 0096 may hold legacy 'random' tier values. RANDOM
    auto-assigned a seat from the tier's sector with no buyer input, and that is
    precisely what BEST_AVAILABLE does for these rows: no deployed row can carry a
    per-category price (category pricing lands in 0097, never before), so
    ``category_prices`` is empty — ``resolve_requested_zone`` then requires no zone
    and ``resolve_seat_price`` falls back to ``tier.price`` for the whole sector.
    Both modes carry the same sector requirement, so nothing else has to change.

    USER_CHOICE would instead demand a ``seat_id`` per item that a RANDOM tier's
    buyers have never sent, 400-ing every purchase until the organizer ships a
    seat picker.
    """
    TicketTier = apps.get_model("events", "TicketTier")
    TicketTier.objects.filter(seat_assignment_mode="random").update(seat_assignment_mode="best_available")


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0098_seat_row_adjacency_backfill"),
    ]

    operations = [
        migrations.RunPython(convert_random_tiers, migrations.RunPython.noop),
    ]
