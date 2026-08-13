"""May *this buyer* take *this many* tickets from this tier?

The buyer-side half of "no": tier access rules, the sale window, and the
per-user ticket limit. The inventory-side half — is there room at all — lives
in :mod:`.capacity`.
"""

import typing as t
from uuid import UUID

from django.db.models import Count
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

if t.TYPE_CHECKING:
    from events.service.batch_ticket_service.context import CartGroup


class PurchaseEligibilityMixin(BatchTicketContext):
    """Tier access rules, the sale window, and the per-user ticket limit."""

    def _assert_purchasable_by(self, tier: TicketTier) -> None:
        """Assert the user is allowed to purchase from this tier.

        Checks the tier's purchasable_by setting and, when restrict_purchase_to_linked_invitations
        is True, verifies the user's invitation links to this specific tier.

        Staff and org owners are exempt from purchasable_by restrictions (consistent with
        CanPurchaseTicket permission). They can always purchase from any tier on their events.

        Args:
            tier: The tier being purchased from.
        """
        PB = TicketTier.PurchasableBy
        if tier.purchasable_by == PB.PUBLIC:
            return

        org = self.event.organization
        if org.is_owner_or_staff(self.user):
            return

        is_member = OrganizationMember.objects.active_only().filter(organization=org, user=self.user).exists()
        invitation = EventInvitation.objects.filter(event=self.event, user=self.user).first()

        def _invited_passes() -> bool:
            if invitation is None:
                return False
            if tier.restrict_purchase_to_linked_invitations:
                return invitation.tiers.filter(pk=tier.pk).exists()
            return True

        if tier.purchasable_by == PB.MEMBERS and is_member:
            return
        if tier.purchasable_by == PB.INVITED and _invited_passes():
            return
        if tier.purchasable_by == PB.INVITED_AND_MEMBERS and (is_member or _invited_passes()):
            return

        raise HttpError(403, str(_("You are not allowed to purchase from this tier.")))

    def _assert_membership_tier_allowed(self, tier: TicketTier) -> None:
        """Assert the buyer holds one of the membership tiers this tier is restricted to.

        Same guard as the membership-tier check in ``ticket_service.get_eligible_tiers``,
        applied to the purchase path (#807): the tier listing hid the tier, but nothing
        stopped a direct checkout call. Semantics are copied verbatim — an empty
        restriction is unrestricted, only an ACTIVE membership counts, and the member's
        tier must be one of the required ones (a member with no tier does not qualify).

        Staff and org owners are exempt, consistent with ``_assert_purchasable_by``.

        Args:
            tier: The tier being purchased from.

        Raises:
            UserIsIneligibleError: If the buyer does not hold a required membership tier.
                Rendered as 400 + the eligibility payload, so the frontend gets the
                ``membership_tier_required`` reason code instead of an opaque 403.
        """
        required_tier_ids = set(tier.restricted_to_membership_tiers.values_list("id", flat=True))
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

    def _assert_sale_window(self, tier: TicketTier) -> None:
        """Sale-window gate — service-authoritative (#846).

        CanPurchaseTicket.has_object_permission held this check but never ran on the
        checkout endpoints (object permissions require get_object_or_exception), and
        TicketSalesGate is any-tier. Every create_batch path enforces it here instead.

        Args:
            tier: The tier being purchased from.

        Raises:
            HttpError: 403 when the tier's sale window is not currently open. No staff
                exemption — matches CanPurchaseTicket, which checked the window before
                any exemption.
        """
        if not tier.can_purchase():
            raise HttpError(403, str(_("You're outside of the sale window.")))

    def get_user_ticket_count(self, tier: TicketTier | None = None) -> int:
        """Get count of user's existing non-cancelled tickets.

        Args:
            tier: When given, count only this tier's tickets. When None, count
                across every tier of the event (the event-cap layer).

        Returns:
            Number of PENDING + ACTIVE tickets the user has.
        """
        qs = Ticket.objects.filter(
            event=self.event,
            user=self.user,
            status__in=[Ticket.TicketStatus.PENDING, Ticket.TicketStatus.ACTIVE],
        )
        if tier is not None:
            qs = qs.filter(tier=tier)
        return qs.count()

    def get_remaining_tickets(
        self,
        tier: TicketTier,
        *,
        event_capacity_remaining: int | None = None,
        user_tier_count: int | None = None,
        user_event_count: int | None = None,
    ) -> int | None:
        """Get how many more tickets user can purchase for this tier.

        Layered per #846 Decision 4: tier and event per-user caps are independent
        ceilings, both enforced when set — the tier cap no longer falls back to the
        event value, and the event cap counts tickets across every tier.

        Calculates the minimum of:
        1. Tier per-user limit, if the tier sets one.
        2. Event per-user limit (counted across all tiers), if the event sets one.
        3. Event capacity remaining (if provided).

        Note: Tier capacity (total_quantity - quantity_sold) is NOT included here
        because it's checked separately by assert_tier_capacity with proper
        "sold out" error handling (429 status code).

        Args:
            tier: The tier to compute remaining tickets for.
            event_capacity_remaining: Remaining event capacity. None means unlimited
                or not provided. Pass this when you've pre-calculated the event's
                remaining capacity to avoid redundant queries.
            user_tier_count: Pre-computed count of user's tickets for this tier.
                If None, will query the database. Pass this when calling in a loop
                to avoid N+1 queries.
            user_event_count: Pre-computed count of user's tickets across the whole
                event. If None, will query the database when the event sets a cap.

        Returns:
            Number of remaining tickets, or None if all limits are unlimited.
        """
        limits: list[int] = []

        if tier.max_tickets_per_user is not None:
            existing = user_tier_count if user_tier_count is not None else self.get_user_ticket_count(tier)
            limits.append(max(0, tier.max_tickets_per_user - existing))

        if self.event.max_tickets_per_user is not None:
            existing = user_event_count if user_event_count is not None else self.get_user_ticket_count()
            limits.append(max(0, self.event.max_tickets_per_user - existing))

        if event_capacity_remaining is not None:
            limits.append(max(0, event_capacity_remaining))

        return min(limits) if limits else None

    def assert_per_user_limits(self, groups: "list[CartGroup]") -> None:
        """Assert every per-user ticket cap the cart is subject to.

        Cart-level event-cap check first (one all-tier count against the whole
        cart's total size), then a per-group tier-cap check. Replaces
        ``validate_batch_size`` — see #846 Decision 4 for why the two layers are
        independent rather than one falling back to the other.

        The tier counts come from ONE grouped aggregate over the capped tiers rather
        than a count per group: the per-group call was an N+1 that grew with cart
        width (#846 review fix). ``get_user_ticket_count`` keeps its single-tier form
        for the other callers.

        Args:
            groups: The cart's groups (one per tier).

        Raises:
            HttpError: 400 when the event cap or a tier cap is exceeded.
        """
        event_cap = self.event.max_tickets_per_user
        if event_cap is not None:
            existing = self.get_user_ticket_count()
            remaining = max(0, event_cap - existing)
            requested = sum(len(g.items) for g in groups)
            if requested > remaining:
                if remaining == 0:
                    raise HttpError(400, str(_("You have reached the maximum number of tickets for this event.")))
                raise HttpError(
                    400,
                    str(_("You can only purchase {remaining} more ticket(s) for this event.")).format(
                        remaining=remaining
                    ),
                )

        capped_tier_ids = [group.tier.pk for group in groups if group.tier.max_tickets_per_user is not None]
        if not capped_tier_ids:
            return
        tier_counts: dict[UUID, int] = {
            row["tier_id"]: row["count"]
            for row in Ticket.objects.filter(
                event=self.event,
                user=self.user,
                tier_id__in=capped_tier_ids,
                status__in=[Ticket.TicketStatus.PENDING, Ticket.TicketStatus.ACTIVE],
            )
            .values("tier_id")
            .annotate(count=Count("id"))
        }

        for group in groups:
            cap = group.tier.max_tickets_per_user
            if cap is None:
                continue
            existing = tier_counts.get(group.tier.pk, 0)
            remaining = max(0, cap - existing)
            if len(group.items) > remaining:
                if remaining == 0:
                    raise HttpError(400, str(_("You have reached the maximum number of tickets for this tier.")))
                raise HttpError(
                    400,
                    str(_("You can only purchase {remaining} more ticket(s) for this tier.")).format(
                        remaining=remaining
                    ),
                )

    def assert_ticket_names(self, items: list[TicketPurchaseItem]) -> None:
        """Enforce Event.require_ticket_names: every item must carry a holder name.

        Runs for buyer-facing paths only; box office stays exempt — it defaults the
        name itself. This is the authoritative gate for every path that reaches
        create_batch. The guest non-online path does NOT reach it until the emailed
        confirmation link is clicked, so it enforces the same rule up front in
        ``handle_guest_ticket_checkout`` — otherwise a nameless guest would get an
        email and a dead link instead of a 400.

        Args:
            items: The batch's ticket purchase items.

        Raises:
            HttpError: 400 when the event requires names and any item lacks one.
        """
        if not self.event.require_ticket_names:
            return
        if any(item.guest_name is None for item in items):
            raise HttpError(400, str(_("This event requires a name on every ticket.")))
