# Generated manually on 2025-11-26

from django.db import migrations


def populate_stripe_account_email(apps, schema_editor):
    """Populate stripe_account_email with owner's email for orgs that have stripe_account_id."""
    Organization = apps.get_model("events", "Organization")

    # Get all organizations that have a stripe_account_id but no stripe_account_email
    orgs_to_update = Organization.objects.filter(
        stripe_account_id__isnull=False
    ).exclude(
        stripe_account_id=""
    ).select_related("owner")

    # Update each organization's stripe_account_email to the owner's email
    for org in orgs_to_update:
        org.stripe_account_email = org.owner.email

    # Bulk update for efficiency
    Organization.objects.bulk_update(orgs_to_update, ["stripe_account_email"])


def reverse_populate_stripe_account_email(apps, schema_editor):
    """Reverse migration: clear stripe_account_email for all orgs."""
    Organization = apps.get_model("events", "Organization")

    # Clear all stripe_account_email values
    Organization.objects.filter(
        stripe_account_email__isnull=False
    ).update(stripe_account_email=None)


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0015_organization_stripe_account_email'),
    ]

    operations = [
        migrations.RunPython(
            populate_stripe_account_email,
            reverse_code=reverse_populate_stripe_account_email,
        ),
    ]
