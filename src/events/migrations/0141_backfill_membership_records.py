import typing as t
from datetime import timedelta

from django.db import migrations

BATCH_SIZE = 1000
FULL_NAME_MAX = 255
# A ban entry created within this window of the member row is the ban (or a registration link) itself.
STRANGER_BAN_WINDOW = timedelta(seconds=60)


def _snapshot_name(user: t.Any) -> str:
    """Same precedence as the runtime snapshot: legal name, then preferred name, then username."""
    return (f"{user.first_name} {user.last_name}".strip() or user.preferred_name or user.username)[:FULL_NAME_MAX]


def forward(apps: t.Any, schema_editor: t.Any) -> None:
    """Backfill the member register from existing ``OrganizationMember`` rows.

    Every (organization, user) pair without any ``MembershipRecord`` gets one STANDARD record
    with source BACKFILL, starting at the member's ``created_at``. ACTIVE/PAUSED rows stay open;
    CANCELLED rows are closed at ``updated_at`` (reason cancelled) and BANNED rows likewise
    (reason expelled). Pairs that already have a record are skipped, so the migration is rerunnable.

    BANNED rows that merely hold a ban on a stranger are skipped. A row counts as a stranger's only
    when ALL hold: a ``Blacklist`` entry for the same (organization, user) was created within a minute
    of the member row, or before it; the row has no tier; the user has no approved/completed
    ``OrganizationMembershipRequest`` in the organization; and no ``MembershipSubscription`` there.
    Any one of those signals shows a real member, who keeps a closed (expelled) record.
    """
    # ponytail: members written by old code during the deploy window are not covered here; the
    # level-triggered mirror heals them on their next save with source STAFF and valid_from=now.
    # Re-running forward() once after deploy is safe (idempotent) but does not fix healed rows.
    # ponytail: the stranger heuristic stays ambiguous for a tier-less member who never applied and
    # never subscribed but was banned within the window (dropped), and for a stranger banned then
    # unbanned (row now CANCELLED, entry deleted: still gets a closed record). The error mode is
    # omitting a closed period, never inventing one; upgrade path is manual correction.
    OrganizationMember = apps.get_model("events", "OrganizationMember")
    MembershipRecord = apps.get_model("events", "MembershipRecord")
    Blacklist = apps.get_model("events", "Blacklist")
    MembershipRequest = apps.get_model("events", "OrganizationMembershipRequest")
    MembershipSubscription = apps.get_model("events", "MembershipSubscription")

    existing = set(MembershipRecord.objects.filter(user__isnull=False).values_list("organization_id", "user_id"))
    ban_entries: dict[tuple[t.Any, t.Any], list[t.Any]] = {}
    for org_id, user_id, created_at in Blacklist.objects.filter(user__isnull=False).values_list(
        "organization_id", "user_id", "created_at"
    ):
        ban_entries.setdefault((org_id, user_id), []).append(created_at)
    applied = set(
        MembershipRequest.objects.filter(status__in=("approved", "completed")).values_list("organization_id", "user_id")
    )
    subscribed = set(MembershipSubscription.objects.values_list("organization_id", "user_id"))

    # Keyset-paged by pk; no .iterator(): server-side cursors break under PgBouncer.
    last_pk = None
    while True:
        members = OrganizationMember.objects.select_related("user", "tier").order_by("pk")
        if last_pk is not None:
            members = members.filter(pk__gt=last_pk)
        chunk = list(members[:BATCH_SIZE])
        if not chunk:
            break
        last_pk = chunk[-1].pk
        _backfill_chunk(chunk, MembershipRecord, existing, ban_entries, applied, subscribed)


def _backfill_chunk(
    chunk: list[t.Any],
    MembershipRecord: t.Any,
    existing: set[tuple[t.Any, t.Any]],
    ban_entries: dict[tuple[t.Any, t.Any], list[t.Any]],
    applied: set[tuple[t.Any, t.Any]],
    subscribed: set[tuple[t.Any, t.Any]],
) -> None:
    closed_reasons = {"cancelled": "cancelled", "banned": "expelled"}
    batch: list[t.Any] = []
    for member in chunk:
        if (member.organization_id, member.user_id) in existing:
            continue
        pair = (member.organization_id, member.user_id)
        if (
            member.status == "banned"
            and member.tier_id is None
            and pair not in applied
            and pair not in subscribed
            and any(entry_at <= member.created_at + STRANGER_BAN_WINDOW for entry_at in ban_entries.get(pair, []))
        ):
            continue
        user = member.user
        reason = closed_reasons.get(member.status)
        batch.append(
            MembershipRecord(
                organization_id=member.organization_id,
                user_id=member.user_id,
                tier=member.tier,
                tier_name=member.tier.name if member.tier else "",
                full_name=_snapshot_name(user),
                email=user.email,
                kind="standard",
                source="backfill",
                valid_from=member.created_at,
                valid_until=max(member.updated_at, member.created_at) if reason else None,
                ended_reason=reason,
            )
        )
        existing.add((member.organization_id, member.user_id))
    if batch:
        MembershipRecord.objects.bulk_create(batch, batch_size=BATCH_SIZE)


class Migration(migrations.Migration):
    dependencies = [("events", "0140_membershiprecord")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
