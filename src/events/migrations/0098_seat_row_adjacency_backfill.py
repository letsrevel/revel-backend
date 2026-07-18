import typing as t

from django.db import migrations


def backfill_row_order_and_adjacency(apps: t.Any, schema_editor: t.Any) -> None:
    """Dense-rank row_order per sector (by row_label) and adjacency_index per row (by number, nulls last)."""
    VenueSeat = apps.get_model("events", "VenueSeat")
    VenueSector = apps.get_model("events", "VenueSector")
    for sector in VenueSector.objects.all().iterator():
        seats = list(VenueSeat.objects.filter(sector=sector))
        row_labels = sorted({s.row_label for s in seats if s.row_label is not None})
        row_rank = {label: i for i, label in enumerate(row_labels)}
        by_row: dict[object, list[t.Any]] = {}
        for s in seats:
            by_row.setdefault(s.row_label, []).append(s)
        to_update = []
        for label, row_seats in by_row.items():
            # numbered seats first (dense rank fixes 1,3,5… gaps), then null-numbered by label
            numbered = sorted((s for s in row_seats if s.number is not None), key=lambda s: (s.number, s.label))
            unnumbered = sorted((s for s in row_seats if s.number is None), key=lambda s: s.label)
            for idx, s in enumerate(numbered + unnumbered):
                s.adjacency_index = idx
                s.row_order = row_rank.get(label, 0)
                to_update.append(s)
        VenueSeat.objects.bulk_update(to_update, ["adjacency_index", "row_order"], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0097_eventseatoverride_pricecategory_seathold_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_row_order_and_adjacency, migrations.RunPython.noop),
    ]
