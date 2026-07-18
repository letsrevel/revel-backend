import typing as t

from django.db import migrations

if t.TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def convert_random_tiers(apps: "Apps", schema_editor: "BaseDatabaseSchemaEditor") -> None:
    """Map removed RANDOM mode: BEST_AVAILABLE when a price category exists, else USER_CHOICE.

    Databases migrating from 0096 may hold legacy 'random' tier values. A
    BEST_AVAILABLE tier without a price category would be unsellable
    (load_candidates draws from the category's seat pool), so category-less
    RANDOM tiers keep their sector semantics as USER_CHOICE.
    """
    TicketTier = apps.get_model("events", "TicketTier")
    TicketTier.objects.filter(seat_assignment_mode="random", price_category__isnull=False).update(
        seat_assignment_mode="best_available"
    )
    TicketTier.objects.filter(seat_assignment_mode="random").update(seat_assignment_mode="user_choice")


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0098_seat_row_adjacency_backfill"),
    ]

    operations = [
        migrations.RunPython(convert_random_tiers, migrations.RunPython.noop),
    ]
