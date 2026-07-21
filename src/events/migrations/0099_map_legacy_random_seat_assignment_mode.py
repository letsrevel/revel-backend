import typing as t

from django.db import migrations

if t.TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def convert_random_tiers(apps: "Apps", schema_editor: "BaseDatabaseSchemaEditor") -> None:
    """Map the removed RANDOM mode onto USER_CHOICE.

    Databases migrating from 0096 may hold legacy 'random' tier values. No deployed
    row can carry a per-category price (category pricing lands in 0097, never before),
    and a BEST_AVAILABLE tier with no priced categories would be unsellable, so every
    legacy RANDOM tier keeps its sector semantics as USER_CHOICE.
    """
    TicketTier = apps.get_model("events", "TicketTier")
    TicketTier.objects.filter(seat_assignment_mode="random").update(seat_assignment_mode="user_choice")


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0098_seat_row_adjacency_backfill"),
    ]

    operations = [
        migrations.RunPython(convert_random_tiers, migrations.RunPython.noop),
    ]
