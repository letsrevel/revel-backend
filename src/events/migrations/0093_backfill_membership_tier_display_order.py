from django.db import migrations


def backfill_display_order(apps, schema_editor):
    """Seed display_order per organization in the current name order (stable ordering on deploy).

    Runs in its own migration (separate from the schema changes in 0092) because
    MembershipTier's FK constraints are DEFERRABLE INITIALLY DEFERRED: modifying rows
    in the same atomic migration as the AddIndex would leave pending trigger events and
    break the deferred CREATE INDEX.
    """
    MembershipTier = apps.get_model("events", "MembershipTier")
    org_ids = MembershipTier.objects.values_list("organization_id", flat=True).distinct()
    for org_id in org_ids:
        tiers = MembershipTier.objects.filter(organization_id=org_id).order_by("name")
        to_update = []
        for index, tier in enumerate(tiers):
            tier.display_order = index
            to_update.append(tier)
        if to_update:
            MembershipTier.objects.bulk_update(to_update, ["display_order"])


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0092_alter_membershiptier_options_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_display_order, migrations.RunPython.noop),
    ]
