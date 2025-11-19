# Manual migration to create default General tiers for organizations
from django.db import migrations


def create_default_tiers_and_assign(apps, schema_editor):
    """Create a default 'General' tier for orgs without tiers and assign to existing members."""
    Organization = apps.get_model("events", "Organization")
    MembershipTier = apps.get_model("events", "MembershipTier")
    OrganizationMember = apps.get_model("events", "OrganizationMember")

    # Find all organizations that have no tiers
    org_ids_with_tiers = MembershipTier.objects.values_list("organization_id", flat=True).distinct()
    orgs_without_tiers = Organization.objects.exclude(id__in=org_ids_with_tiers)

    # Bulk create "General" tiers for organizations without any tiers
    tiers_to_create = [
        MembershipTier(
            organization_id=org.id,
            name="General membership",
        )
        for org in orgs_without_tiers
    ]
    created_tiers = MembershipTier.objects.bulk_create(tiers_to_create)

    # Create a mapping of organization_id -> tier for fast lookup
    org_to_tier = {tier.organization_id: tier for tier in created_tiers}

    # Get all members for organizations that just got default tiers
    # and don't already have a tier assigned
    members_to_update = OrganizationMember.objects.filter(
        organization_id__in=org_to_tier.keys(),
        tier__isnull=True,
    )

    # Assign the appropriate tier to each member
    for member in members_to_update:
        member.tier = org_to_tier[member.organization_id]

    # Bulk update all members at once
    OrganizationMember.objects.bulk_update(members_to_update, ["tier"])


def reverse_migration(apps, schema_editor):
    """Remove the default 'General' tiers created by this migration."""
    MembershipTier = apps.get_model("events", "MembershipTier")
    OrganizationMember = apps.get_model("events", "OrganizationMember")

    # Find all "General" tiers
    general_tiers = MembershipTier.objects.filter(name="General")

    # Unassign members from these tiers
    OrganizationMember.objects.filter(tier__in=general_tiers).update(tier=None)

    # Delete the General tiers
    general_tiers.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0012_organizationmember_status_membershiptier_and_more"),
    ]

    operations = [
        migrations.RunPython(create_default_tiers_and_assign, reverse_migration),
    ]
