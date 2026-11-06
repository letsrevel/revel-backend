import typing as t

from django.db import migrations

BATCH_SIZE = 1000


def forward(apps: t.Any, schema_editor: t.Any) -> None:
    """Backfill the member register from existing ``OrganizationMember`` rows.

    Every (organization, user) pair without any ``MembershipRecord`` gets one STANDARD record
    with source BACKFILL, starting at the member's ``created_at``. ACTIVE/PAUSED rows stay open;
    CANCELLED rows are closed at ``updated_at`` (reason cancelled) and BANNED rows likewise
    (reason expelled). Pairs that already have a record are skipped, so the migration is rerunnable.

    # ponytail: members written by old code during the deploy window are not covered here;
    # the level-triggered mirror heals them on their next save.
    """
    OrganizationMember = apps.get_model("events", "OrganizationMember")
    MembershipRecord = apps.get_model("events", "MembershipRecord")

    existing = set(MembershipRecord.objects.filter(user__isnull=False).values_list("organization_id", "user_id"))
    closed_reasons = {"cancelled": "cancelled", "banned": "expelled"}

    batch: list[t.Any] = []
    # No .iterator(): server-side cursors break under PgBouncer.
    for member in OrganizationMember.objects.select_related("user", "tier"):
        if (member.organization_id, member.user_id) in existing:
            continue
        user = member.user
        full_name = user.preferred_name or f"{user.first_name} {user.last_name}".strip() or user.username
        reason = closed_reasons.get(member.status)
        batch.append(
            MembershipRecord(
                organization_id=member.organization_id,
                user_id=member.user_id,
                tier=member.tier,
                tier_name=member.tier.name if member.tier else "",
                full_name=full_name,
                email=user.email,
                kind="standard",
                source="backfill",
                valid_from=member.created_at,
                valid_until=member.updated_at if reason else None,
                ended_reason=reason,
            )
        )
        existing.add((member.organization_id, member.user_id))
        if len(batch) >= BATCH_SIZE:
            MembershipRecord.objects.bulk_create(batch, batch_size=BATCH_SIZE)
            batch = []
    if batch:
        MembershipRecord.objects.bulk_create(batch, batch_size=BATCH_SIZE)


class Migration(migrations.Migration):
    dependencies = [("events", "0140_membershiprecord")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
