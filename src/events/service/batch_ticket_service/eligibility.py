"""May *this buyer* take *this many* tickets from this tier?

The buyer-side half of "no": tier access rules and the per-user ticket limit.
The inventory-side half — is there room at all — lives in :mod:`.capacity`.
"""

from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.models import EventInvitation, OrganizationMember, Ticket, TicketTier
from events.schema import TicketPurchaseItem
from events.service.batch_ticket_service.context import BatchTicketContext
from events.service.event_manager import (
    EventUserEligibility,
    NextStep,
    Reasons,
    UserIsIneligibleError,
)


class PurchaseEligibilityMixin(BatchTicketContext):
    """Tier access rules and the per-user ticket limit."""

    def _assert_purchasable_by(self) -> None:
        """Assert the user is allowed to purchase from this tier.

        Checks the tier's purchasable_by setting and, when restrict_purchase_to_linked_invitations
        is True, verifies the user's invitation links to this specific tier.

        Staff and org owners are exempt from purchasable_by restrictions (consistent with
        CanPurchaseTicket permission). They can always purchase from any tier on their events.
        """
        PB = TicketTier.PurchasableBy
        if self.tier.purchasable_by == PB.PUBLIC:
            return

        org = self.event.organization
        if org.is_owner_or_staff(self.user):
            return

        is_member = OrganizationMember.objects.active_only().filter(organization=org, user=self.user).exists()
        invitation = EventInvitation.objects.filter(event=self.event, user=self.user).first()

        def _invited_passes() -> bool:
            if invitation is None:
                return False
            if self.tier.restrict_purchase_to_linked_invitations:
                return invitation.tiers.filter(pk=self.tier.pk).exists()
            return True

        if self.tier.purchasable_by == PB.MEMBERS and is_member:
            return
        if self.tier.purchasable_by == PB.INVITED and _invited_passes():
            return
        if self.tier.purchasable_by == PB.INVITED_AND_MEMBERS and (is_member or _invited_passes()):
            return

        raise HttpError(403, str(_("You are not allowed to purchase from this tier.")))

    def _assert_membership_tier_allowed(self) -> None:
        """Assert the buyer holds one of the membership tiers this tier is restricted to.

        Same guard as the membership-tier check in ``ticket_service.get_eligible_tiers``,
        applied to the purchase path (#807): the tier listing hid the tier, but nothing
        stopped a direct checkout call. Semantics are copied verbatim — an empty
        restriction is unrestricted, only an ACTIVE membership counts, and the member's
        tier must be one of the required ones (a member with no tier does not qualify).

        Staff and org owners are exempt, consistent with ``_assert_purchasable_by``.

        Raises:
            UserIsIneligibleError: If the buyer does not hold a required membership tier.
                Rendered as 400 + the eligibility payload, so the frontend gets the
                ``membership_tier_required`` reason code instead of an opaque 403.
        """
        required_tier_ids = set(self.tier.restricted_to_membership_tiers.values_list("id", flat=True))
        if not required_tier_ids:
            return

        org = self.event.organization
        if org.is_owner_or_staff(self.user):
            return

        membership = OrganizationMember.objects.active_only().filter(organization=org, user=self.user).first()
        if membership is not None and membership.tier_id in required_tier_ids:
            return

        # UPGRADE_MEMBERSHIP for members and non-members alike: the only way through is
        # to hold one of the named tiers. BECOME_MEMBER would be a dead end — a plain
        # membership request grants no tier, so it would not unblock the purchase.
        raise UserIsIneligibleError(
            message="Membership tier required.",
            eligibility=EventUserEligibility(
                allowed=False,
                event_id=self.event.id,
                reason=str(_(Reasons.MEMBERSHIP_TIER_REQUIRED)),
                reason_code=Reasons.MEMBERSHIP_TIER_REQUIRED.code,
                next_step=NextStep.UPGRADE_MEMBERSHIP,
            ),
        )

    def get_user_ticket_count(self) -> int:
        """Get count of user's existing non-cancelled tickets for this tier.

        Returns:
            Number of PENDING + ACTIVE tickets the user has for this tier.
        """
        return Ticket.objects.filter(
            event=self.event,
            tier=self.tier,
            user=self.user,
            status__in=[Ticket.TicketStatus.PENDING, Ticket.TicketStatus.ACTIVE],
        ).count()

    def get_remaining_tickets(
        self,
        event_capacity_remaining: int | None = None,
        user_ticket_count: int | None = None,
    ) -> int | None:
        """Get how many more tickets user can purchase for this tier.

        Calculates the minimum of:
        1. Per-user limit (tier-specific or event-level fallback)
        2. Event capacity remaining (if provided)

        Note: Tier capacity (total_quantity - quantity_sold) is NOT included here
        because it's checked separately by assert_tier_capacity with proper
        "sold out" error handling (429 status code).

        Args:
            event_capacity_remaining: Remaining event capacity. None means unlimited
                or not provided. Pass this when you've pre-calculated the event's
                remaining capacity to avoid redundant queries.
            user_ticket_count: Pre-computed count of user's tickets for this tier.
                If None, will query the database. Pass this when calling in a loop
                to avoid N+1 queries.

        Returns:
            Number of remaining tickets, or None if all limits are unlimited.
        """
        limits: list[int] = []

        # 1. Per-user limit
        max_allowed = self.tier.max_tickets_per_user
        if max_allowed is None:
            max_allowed = self.event.max_tickets_per_user
        if max_allowed is not None:
            existing = user_ticket_count if user_ticket_count is not None else self.get_user_ticket_count()
            limits.append(max(0, max_allowed - existing))

        # 2. Event capacity limit (if provided)
        if event_capacity_remaining is not None:
            limits.append(max(0, event_capacity_remaining))

        return min(limits) if limits else None

    def validate_batch_size(self, requested: int) -> None:
        """Validate that the batch size doesn't exceed limits.

        Args:
            requested: Number of tickets being requested.

        Raises:
            HttpError: If the batch size exceeds the allowed limit.
        """
        remaining = self.get_remaining_tickets()
        if remaining is not None and requested > remaining:
            if remaining == 0:
                raise HttpError(
                    400,
                    str(_("You have reached the maximum number of tickets for this tier.")),
                )
            raise HttpError(
                400,
                str(_("You can only purchase {remaining} more ticket(s) for this tier.")).format(remaining=remaining),
            )

    def assert_ticket_names(self, items: list[TicketPurchaseItem]) -> None:
        """Enforce Event.require_ticket_names: every item must carry a holder name.

        Runs for buyer-facing paths only (auth + guest both funnel through
        create_batch); box office stays exempt — it defaults the name itself.

        Args:
            items: The batch's ticket purchase items.

        Raises:
            HttpError: 400 when the event requires names and any item lacks one.
        """
        if not self.event.require_ticket_names:
            return
        if any(item.guest_name is None for item in items):
            raise HttpError(400, str(_("This event requires a name on every ticket.")))
