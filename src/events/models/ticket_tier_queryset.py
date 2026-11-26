"""Queryset and manager of TicketTier: the visibility rules for who can see a tier."""

import typing as t

from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.db import models
from django.db.models import F, Q

from events.utils.membership_access import visible_memberships
from events.utils.visibility import get_invited_event_ids, get_valid_member_org_ids, owner_or_staff_q

if t.TYPE_CHECKING:
    from accounts.models import RevelUser
    from events.models.event import Event
    from events.models.invitation import EventToken
    from events.models.ticket import TicketTier  # noqa: F401  (resolves the "TicketTier" string annotations)


class TicketTierQuerySet(models.QuerySet["TicketTier"]):
    def with_venue_and_sector(self) -> t.Self:
        """Select venue and sector for serialization (not for transactional queries)."""
        return self.select_related("venue", "sector")

    def with_event_compliance(self) -> t.Self:
        """Select what the ``invoicing_available`` flag reads: the event's org, venue and cities."""
        return self.select_related("event__organization__city", "event__city", "event__venue__city")

    def for_user(self, user: "RevelUser | AnonymousUser") -> t.Self:
        """Return ticket tiers visible to a given user, combining event and tier-level access.

        Membership status handling:
        - CANCELLED users: Treated as if they have no membership (no access to member-only tiers)
        - BANNED users: Inherit banned status from Event.for_user (won't see the org's events,
          except ones they hold a ticket for)
        """
        from .event import Event

        qs = self.select_related("event", "event__organization")

        # --- Anonymous User ---
        if user.is_anonymous:
            return qs.filter(
                visibility__in=self.model.Visibility.publicly_accessible(),
                event__visibility__in=Event.Visibility.publicly_accessible(),
                event__status=Event.EventStatus.OPEN,
            )

        if user.is_superuser:
            return qs

        # --- Authenticated User ---
        # 1. Get all events this user is allowed to see. This is the source of truth.
        # Event.for_user already handles banned users (no events from banned orgs, except ticketed ones)
        visible_event_ids = Event.objects.for_user(user, include_past=True).values_list("id", flat=True)

        # Base filter: only consider tiers on events the user can see.
        base_q = Q(event_id__in=visible_event_ids)

        # If user is owner/staff of the org, they can see all tiers on that event.
        is_owner_or_staff = owner_or_staff_q(user, org_lookup="event__organization")
        # Django staff are not the same as org staff, but we can treat them like superusers here.
        if user.is_staff:
            return qs.filter(base_q).distinct()

        # For regular users, apply tier-specific visibility rules ON TOP of visible events.
        is_public_tier = Q(visibility__in=self.model.Visibility.publicly_accessible())

        # Only valid members (not cancelled, not banned) can see member-only tiers
        is_member_tier = Q(
            visibility=self.model.Visibility.MEMBERS_ONLY,
            event__organization_id__in=get_valid_member_org_ids(user),
        )

        # Unrestricted private tiers: any invitation to the event grants visibility
        unrestricted_private = Q(
            visibility=self.model.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=False,
            event_id__in=get_invited_event_ids(user),
        )
        # Restricted private tiers: user's invitation must link to THIS tier AND be for the same event
        restricted_private = Q(
            visibility=self.model.Visibility.PRIVATE,
            restrict_visibility_to_linked_invitations=True,
            event_invitations__user=user,
            event_invitations__event_id=F("event_id"),
        )
        is_private_tier = unrestricted_private | restricted_private

        # A regular user can see a tier if it's on a visible event AND...
        # ...it's a public tier, OR
        # ...it's a member tier and they are a valid member, OR
        # ...it's a private tier and they are invited (with tier-link check if restricted).
        tier_visibility_q = is_public_tier | is_member_tier | is_private_tier

        # Staff/owners of other orgs might also see some of these events if public,
        # so we also explicitly include tiers for events they are staff/owner of.
        final_q = base_q & (tier_visibility_q | is_owner_or_staff)

        return qs.filter(final_q).distinct()

    def for_visible_event(
        self, event: "Event", user: "RevelUser | AnonymousUser", event_token: "EventToken | None" = None
    ) -> t.Self:
        """Return ticket tiers for an already visibility-checked event.

        Use this when you already have an event that passed visibility checks via Event.for_user().
        This avoids redundant Event.for_user() queries by only applying tier-level visibility rules.

        Args:
            event: An event that already passed visibility checks for this user.
            user: The user to check tier visibility for.
            event_token: An invitation link the request carries. For an anonymous user it
                grants the private-tier visibility the invitation it would create grants,
                so a guest can reach an invitation-gated tier before the claim exists.

        Returns:
            QuerySet of tiers the user can see for this specific event.
        """
        from .invitation import EventInvitation

        qs = self.filter(event=event)

        # Superusers and Django staff see all tiers
        if not user.is_anonymous and (user.is_superuser or user.is_staff):
            return qs

        # Anonymous users: only publicly accessible tiers (PUBLIC + UNLISTED), plus the
        # private tiers a granting invitation link for this event would unlock — the same
        # rule as the invitation branch below, keyed on the token's tier links instead.
        if user.is_anonymous:
            is_public_tier = Q(visibility__in=self.model.Visibility.publicly_accessible())
            if event_token is None or not event_token.grants_invitation or event_token.event_id != event.id:
                return qs.filter(is_public_tier)
            token_tier_ids = {tier.id for tier in event_token.ticket_tiers.all()}
            unrestricted_private = Q(
                visibility=self.model.Visibility.PRIVATE,
                restrict_visibility_to_linked_invitations=False,
            )
            restricted_private = (
                Q(
                    visibility=self.model.Visibility.PRIVATE,
                    restrict_visibility_to_linked_invitations=True,
                    pk__in=token_tier_ids,
                )
                if token_tier_ids
                else Q(pk__isnull=True)
            )
            return qs.filter(is_public_tier | unrestricted_private | restricted_private)

        # Check if user is org owner or staff - they see all tiers
        if event.organization.is_owner_or_staff(user):
            return qs

        # Regular user: apply tier visibility rules
        is_public_tier = Q(visibility__in=self.model.Visibility.publicly_accessible())

        # Member-only tiers: check if user is valid member of this org
        is_member = visible_memberships().filter(user=user, organization=event.organization).exists()
        is_member_tier = Q(visibility=self.model.Visibility.MEMBERS_ONLY) if is_member else Q(pk__isnull=True)

        # Private tiers: check invitation and tier-link restriction
        invitation = EventInvitation.objects.filter(user=user, event=event).first()
        if invitation:
            linked_tier_ids = set(invitation.tiers.values_list("id", flat=True))
            # Unrestricted private tiers: any invitation grants visibility
            unrestricted_private = Q(
                visibility=self.model.Visibility.PRIVATE,
                restrict_visibility_to_linked_invitations=False,
            )
            # Restricted private tiers: invitation must link to this tier
            restricted_private = (
                Q(
                    visibility=self.model.Visibility.PRIVATE,
                    restrict_visibility_to_linked_invitations=True,
                    pk__in=linked_tier_ids,
                )
                if linked_tier_ids
                else Q(pk__isnull=True)
            )
            is_private_tier = unrestricted_private | restricted_private
        else:
            is_private_tier = Q(pk__isnull=True)  # No invitation = no private tiers

        return qs.filter(is_public_tier | is_member_tier | is_private_tier)


class TicketTierManager(models.Manager["TicketTier"]):
    def get_queryset(self) -> TicketTierQuerySet:
        """Get base queryset for ticket tiers."""
        return TicketTierQuerySet(self.model, using=self._db).prefetch_related(
            "restricted_to_membership_tiers", "excluded_membership_tiers"
        )

    def with_venue_and_sector(self) -> TicketTierQuerySet:
        """Return queryset with venue and sector prefetched for API serialization."""
        return self.get_queryset().with_venue_and_sector()

    def for_user(self, user: "RevelUser | AnonymousUser") -> TicketTierQuerySet:
        """Return ticket tiers visible to a given user, combining event and tier-level access."""
        return self.get_queryset().for_user(user)

    def for_visible_event(
        self, event: "Event", user: "RevelUser | AnonymousUser", event_token: "EventToken | None" = None
    ) -> TicketTierQuerySet:
        """Return ticket tiers for an already visibility-checked event."""
        return self.get_queryset().for_visible_event(event, user, event_token=event_token)
