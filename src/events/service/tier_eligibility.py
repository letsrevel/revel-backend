"""Which ticket tiers may this user see and buy? (tier-level purchasability).

Split out of :mod:`events.service.ticket_service`. The tier listing, the user's event
status and the anonymous listing read these; checkout re-asserts the same rules in
:mod:`events.service.batch_ticket_service.eligibility`.
"""

from uuid import UUID

from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventInvitation, EventToken, OrganizationMember, TicketTier
from events.models.mixins import VisibilityMixin
from events.utils.membership_access import is_eligible_status, is_visible_status


def get_eligible_tiers(event: Event, user: RevelUser) -> list[TicketTier]:
    """Get ticket tiers the user is eligible to purchase from.

    A tier is eligible if ALL of these pass:
    1. Visibility check: user can see the tier (PUBLIC, MEMBERS_ONLY for members,
       PRIVATE for invited users, or user is org staff/owner)
    2. Sales window: currently within the tier's sales window
    3. Purchasable_by check: user is allowed to purchase (PUBLIC, MEMBERS for members,
       INVITED for invitees, INVITED_AND_MEMBERS for either)
    4. Membership tier restriction: user has the required membership tier (if restricted)
    5. Member exclusion: for "non-members only" tiers, user is not an eligible member
       (of an excluded membership tier, when the tier names any)

    Note:
        This function differs from EventManager.check_eligibility() in purpose:
        - check_eligibility() determines if user can ACCESS the event at all (blacklist,
          event status, invitation requirements, questionnaires, etc.)
        - get_eligible_tiers() determines which TIERS user can purchase from, assuming
          they already have event access.

        The caller should ensure event.organization is prefetched to avoid an extra query.

    Args:
        event: The event to check tiers for. Should have organization prefetched.
        user: The user to check eligibility for.

    Returns:
        List of TicketTier objects the user is eligible to purchase.
    """
    now = timezone.now()
    org = event.organization

    # Pre-fetch user context (single queries, not in loop)
    is_staff_or_owner = org.is_owner_or_staff(user)

    # One membership query (any status): visible statuses may SEE member tiers, only eligible may BUY
    membership = OrganizationMember.objects.filter(organization=org, user=user).only("status", "tier_id").first()
    status = membership.status if membership else None
    can_see_member_tiers = is_visible_status(status)
    is_member = is_eligible_status(status)
    user_membership_tier_ids = {membership.tier_id} if membership and is_member and membership.tier_id else set()

    # Check if user is invited to this event and get linked tier IDs
    invitation = EventInvitation.objects.prefetch_related("tiers").filter(event=event, user=user).first()
    is_invited = invitation is not None
    invitation_tier_ids = set(invitation.tiers.values_list("id", flat=True)) if invitation else set()

    eligible: list[TicketTier] = []

    # Prefetch the membership-tier links to avoid N+1 queries
    for tier in event.ticket_tiers.prefetch_related(
        "restricted_to_membership_tiers", "excluded_membership_tiers"
    ).all():
        # 1. Check visibility
        if not _check_tier_visibility(tier, is_staff_or_owner, can_see_member_tiers, is_invited, invitation_tier_ids):
            continue

        # 2. Check sales window / pause
        if tier.sales_paused:
            continue
        if tier.sales_start_at and now < tier.sales_start_at:
            continue
        if tier.sales_end_at and now > tier.sales_end_at:
            continue

        # 3. Check purchasable_by (staff/owners are exempt, consistent with _assert_purchasable_by)
        if not is_staff_or_owner:
            if not _check_purchasable_by(tier, is_member, is_invited, invitation_tier_ids):
                continue

            # 4. Check membership tier restriction
            required_tier_ids = {mt.id for mt in tier.restricted_to_membership_tiers.all()}
            if required_tier_ids and not (user_membership_tier_ids & required_tier_ids):
                continue

            # 5. Check member exclusion ("non-members only" tiers)
            if is_member_excluded(tier, membership if is_member else None):
                continue

        eligible.append(tier)

    return eligible


def is_member_excluded(tier: TicketTier, membership: OrganizationMember | None) -> bool:
    """Whether a "non-members only" tier shuts this buyer out because of their membership.

    Staff/owner exemption is the caller's, as for :func:`_check_purchasable_by`.

    Args:
        tier: The tier being purchased from. ``excluded_membership_tiers`` is read through the
            manager's prefetch.
        membership: The buyer's ELIGIBLE membership (callers pass None when it is not eligible).

    Returns:
        True if the tier is non-members-only and the membership is excluded: any eligible member
        when the tier names no excluded membership tiers, otherwise only members of those tiers.
    """
    if tier.purchasable_by != TicketTier.PurchasableBy.NON_MEMBERS or membership is None:
        return False
    excluded_ids = {mt.id for mt in tier.excluded_membership_tiers.all()}
    return not excluded_ids or membership.tier_id in excluded_ids


def _check_tier_visibility(
    tier: TicketTier,
    is_staff_or_owner: bool,
    can_see_member_tiers: bool,
    is_invited: bool,
    invitation_tier_ids: set[UUID],
) -> bool:
    """Check if user can see the tier based on visibility settings.

    Args:
        tier: The ticket tier to check.
        is_staff_or_owner: Whether user is org staff or owner.
        can_see_member_tiers: Whether user's membership may see member-only content.
        is_invited: Whether user has an invitation to the event.
        invitation_tier_ids: Set of tier IDs linked to user's invitation.

    Returns:
        True if user can see the tier.
    """
    # Staff/owners can see all tiers
    if is_staff_or_owner:
        return True

    visibility = tier.visibility

    if visibility in VisibilityMixin.Visibility.publicly_accessible():
        return True

    if visibility == VisibilityMixin.Visibility.MEMBERS_ONLY:
        return can_see_member_tiers

    if visibility == VisibilityMixin.Visibility.PRIVATE:
        if tier.restrict_visibility_to_linked_invitations:
            return tier.id in invitation_tier_ids
        return is_invited

    # STAFF_ONLY - only staff/owner (already checked above)
    return False


def anonymous_can_purchase(tier: TicketTier, event: Event, event_token: EventToken | None) -> bool:
    """Whether an anonymous viewer may buy from this tier.

    Public tiers always. Otherwise only what a granting invitation link for this
    event would unlock once guest checkout claims it — the same rule as
    :func:`_check_purchasable_by` for an invited non-member, keyed on the token's
    tier links instead of an invitation's. Keeps the listing's ``can_purchase``
    honest for link holders, so the frontend need not second-guess it.

    Args:
        tier: The tier being listed.
        event: The event the tier belongs to.
        event_token: The invitation link the request carries, if any.

    Returns:
        True if an anonymous viewer carrying ``event_token`` can purchase from the tier.
    """
    # A paused tier is unbuyable for everyone, so the listing must not advertise it (mirrors
    # get_eligible_tiers step 2). The sales *window* is deliberately still not checked here —
    # that gap predates the kill switch and is tracked separately.
    if tier.sales_paused:
        return False
    # A membership-tier restriction can never be met without an account (mirrors
    # get_eligible_tiers step 4). ``.all()`` reads the manager's prefetch.
    if tier.restricted_to_membership_tiers.all():
        return False
    # No account means no membership, so a non-members-only tier is open like a public one.
    if tier.purchasable_by in (TicketTier.PurchasableBy.PUBLIC, TicketTier.PurchasableBy.NON_MEMBERS):
        return True
    if event_token is None or not event_token.grants_invitation or event_token.event_id != event.id:
        return False
    token_tier_ids = {linked.id for linked in event_token.ticket_tiers.all()}
    return _check_purchasable_by(tier, is_member=False, is_invited=True, invitation_tier_ids=token_tier_ids)


def _check_purchasable_by(
    tier: TicketTier,
    is_member: bool,
    is_invited: bool,
    invitation_tier_ids: set[UUID],
) -> bool:
    """Check if a regular user is allowed to purchase from this tier based on purchasable_by setting.

    Note: Staff/owners are exempted by the caller (get_eligible_tiers and _assert_purchasable_by)
    before this function is reached. This function only evaluates membership/invitation rules.

    Args:
        tier: The ticket tier to check.
        is_member: Whether user is an active org member.
        is_invited: Whether user has an invitation to the event.
        invitation_tier_ids: Set of tier IDs linked to user's invitation.

    Returns:
        True if user can purchase from this tier.
    """
    purchasable_by = tier.purchasable_by

    if purchasable_by == TicketTier.PurchasableBy.PUBLIC:
        return True

    # Member exclusion is a separate step that needs the membership tier: see is_member_excluded.
    if purchasable_by == TicketTier.PurchasableBy.NON_MEMBERS:
        return True

    if purchasable_by == TicketTier.PurchasableBy.MEMBERS:
        return is_member

    if purchasable_by == TicketTier.PurchasableBy.INVITED:
        if tier.restrict_purchase_to_linked_invitations:
            return tier.id in invitation_tier_ids
        return is_invited

    if purchasable_by == TicketTier.PurchasableBy.INVITED_AND_MEMBERS:
        invited_ok = is_invited
        if tier.restrict_purchase_to_linked_invitations:
            invited_ok = tier.id in invitation_tier_ids
        return is_member or invited_ok

    return False
