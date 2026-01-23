"""Service for managing user preferences."""

import typing as t
from dataclasses import dataclass, field
from uuid import UUID

from django.utils import timezone
from pydantic import BaseModel

from accounts.models import RevelUser
from events.models import Event, EventInvitation, EventRSVP, GeneralUserPreferences, OrganizationMember, Ticket
from events.service import update_db_instance
from events.service.location_service import invalidate_user_location_cache
from events.tasks import build_attendee_visibility_flags


@dataclass
class VisibilityContext:
    """Pre-loaded context for batch visibility resolution.

    This eliminates N+1 queries by loading all relationship data upfront.
    """

    event: Event
    owner_id: UUID
    staff_ids: set[UUID]
    # Sets of user IDs for O(1) lookup
    invited_user_ids: set[UUID] = field(default_factory=set)
    ticket_user_ids: set[UUID] = field(default_factory=set)
    rsvp_user_ids: set[UUID] = field(default_factory=set)
    org_member_ids: set[UUID] = field(default_factory=set)

    @classmethod
    def for_event(cls, event: Event, owner_id: UUID, staff_ids: set[UUID]) -> "VisibilityContext":
        """Create a VisibilityContext with all data prefetched for an event.

        Args:
            event: The event to build context for
            owner_id: The organization owner ID
            staff_ids: Set of organization staff IDs

        Returns:
            VisibilityContext with all relationship data loaded
        """
        org_id = event.organization_id

        # Prefetch all relationship data in 4 queries (instead of N per pair)
        invited_user_ids = set(EventInvitation.objects.filter(event=event).values_list("user_id", flat=True))
        ticket_user_ids = set(
            Ticket.objects.filter(event=event, status=Ticket.TicketStatus.ACTIVE).values_list("user_id", flat=True)
        )
        rsvp_user_ids = set(
            EventRSVP.objects.filter(event=event, status=EventRSVP.RsvpStatus.YES).values_list("user_id", flat=True)
        )
        org_member_ids = set(
            OrganizationMember.objects.filter(organization_id=org_id).values_list("user_id", flat=True)
        )

        return cls(
            event=event,
            owner_id=owner_id,
            staff_ids=staff_ids,
            invited_user_ids=invited_user_ids,
            ticket_user_ids=ticket_user_ids,
            rsvp_user_ids=rsvp_user_ids,
            org_member_ids=org_member_ids,
        )

    def is_viewer_invited_or_attending(self, viewer_id: UUID) -> bool:
        """Check if viewer is invited or attending the event. O(1) lookup."""
        return (
            viewer_id in self.invited_user_ids or viewer_id in self.ticket_user_ids or viewer_id in self.rsvp_user_ids
        )

    def are_both_org_members(self, viewer_id: UUID, target_id: UUID) -> bool:
        """Check if both viewer and target are organization members. O(1) lookup."""
        return viewer_id in self.org_member_ids and target_id in self.org_member_ids


def set_general_preferences(
    instance: GeneralUserPreferences,
    payload: BaseModel,
) -> GeneralUserPreferences:
    """Update general user preferences.

    Args:
        instance: The preferences instance to update
        payload: The update payload

    Returns:
        Updated preferences instance
    """
    # Track old city_id before update (for location cache invalidation)
    old_city_id = getattr(instance, "city_id", None)

    updated_instance = update_db_instance(instance, payload, exclude_unset=False, exclude_defaults=False)

    # Track if visibility changed
    visibility_changed = False
    if payload and "show_me_on_attendee_list" in payload.model_fields_set:
        old_value = getattr(instance, "show_me_on_attendee_list", None)
        new_value = getattr(updated_instance, "show_me_on_attendee_list", None)
        visibility_changed = old_value != new_value

    # Invalidate location cache if city_id changed and was explicitly set in payload
    if payload and "city_id" in payload.model_fields_set:
        new_city_id = getattr(updated_instance, "city_id", None)
        if old_city_id != new_city_id:
            invalidate_user_location_cache(updated_instance.user_id)

    # Trigger visibility flags rebuild if visibility changed
    if visibility_changed:
        trigger_visibility_flags_for_user(updated_instance.user.pk)

    return updated_instance


def trigger_visibility_flags_for_user(user_id: UUID) -> None:
    """Dispatch build_attendee_visibility_flags for all future events the user is attending.

    Args:
        user_id: The user ID
    """
    event_ids = (
        Ticket.objects.filter(
            user_id=user_id,
            status=Ticket.TicketStatus.ACTIVE,
            event__start__gte=timezone.now(),
        )
        .values_list("event_id", flat=True)
        .union(
            EventRSVP.objects.filter(
                user_id=user_id,
                status=EventRSVP.RsvpStatus.YES,
                event__start__gte=timezone.now(),
            ).values_list("event_id", flat=True)
        )
    )
    for event_id in event_ids:
        build_attendee_visibility_flags.delay(str(event_id))


def resolve_visibility_fast(
    viewer: RevelUser,
    target: RevelUser,
    context: VisibilityContext,
) -> bool:
    """Resolve visibility using pre-loaded context. O(1) lookups, no DB queries.

    This is the optimized version that should be used in batch operations.

    Args:
        viewer: The user viewing the attendee list
        target: The target attendee being viewed
        context: Pre-loaded VisibilityContext with all relationship data

    Returns:
        True if target should be visible to viewer
    """
    # Staff and owners can see everyone
    if viewer.id == context.owner_id or viewer.id in context.staff_ids:
        return True

    # Get target's visibility preference (should be prefetched via select_related)
    target_prefs = getattr(target, "general_preferences", None)
    if not target_prefs:
        return False

    visibility = target_prefs.show_me_on_attendee_list

    # Check visibility setting
    if visibility == GeneralUserPreferences.VisibilityPreference.ALWAYS:
        return True

    if visibility == GeneralUserPreferences.VisibilityPreference.NEVER:
        return False

    # For conditional visibility, use O(1) set lookups
    match visibility:
        case GeneralUserPreferences.VisibilityPreference.TO_MEMBERS:
            return context.are_both_org_members(viewer.id, target.id)
        case GeneralUserPreferences.VisibilityPreference.TO_INVITEES:
            return context.is_viewer_invited_or_attending(viewer.id)
        case GeneralUserPreferences.VisibilityPreference.TO_BOTH:
            return context.is_viewer_invited_or_attending(viewer.id) or context.are_both_org_members(
                viewer.id, target.id
            )
        case _:
            return False


def resolve_visibility(
    viewer: RevelUser,
    target: RevelUser,
    event: Event,
    owner_id: UUID,
    staff_ids: set[UUID],
    context: t.Optional[VisibilityContext] = None,
) -> bool:
    """Resolve whether an attendee (target) is visible to another user (viewer).

    For batch operations, pass a pre-loaded VisibilityContext to avoid N+1 queries.
    For single lookups, this will fall back to individual queries.

    Args:
        viewer: The user viewing the attendee list
        target: The target attendee being viewed
        event: The event context
        owner_id: The organization owner ID
        staff_ids: Set of organization staff IDs
        context: Optional pre-loaded VisibilityContext for batch operations

    Returns:
        True if target should be visible to viewer
    """
    # If context provided, use fast path
    if context is not None:
        return resolve_visibility_fast(viewer, target, context)

    # Staff and owners can see everyone
    if viewer.id == owner_id or viewer.id in staff_ids:
        return True

    # Get target's visibility preference
    target_prefs = getattr(target, "general_preferences", None)
    if not target_prefs:
        return False

    visibility = target_prefs.show_me_on_attendee_list
    org_id = event.organization_id

    # Check visibility setting
    if visibility == GeneralUserPreferences.VisibilityPreference.ALWAYS:
        return True

    if visibility == GeneralUserPreferences.VisibilityPreference.NEVER:
        return False

    # For conditional visibility, check viewer's relationship (individual queries - N+1 if looped)
    is_invited_or_attending = (
        EventInvitation.objects.filter(event=event, user=viewer).exists()
        or Ticket.objects.filter(event=event, user=viewer, status=Ticket.TicketStatus.ACTIVE).exists()
        or EventRSVP.objects.filter(event=event, user=viewer, status=EventRSVP.RsvpStatus.YES).exists()
    )

    is_same_org_member = (
        OrganizationMember.objects.filter(
            organization_id=org_id,
            user=viewer,
        ).exists()
        and OrganizationMember.objects.filter(
            organization_id=org_id,
            user=target,
        ).exists()
    )

    match visibility:
        case GeneralUserPreferences.VisibilityPreference.TO_MEMBERS:
            return is_same_org_member
        case GeneralUserPreferences.VisibilityPreference.TO_INVITEES:
            return is_invited_or_attending
        case GeneralUserPreferences.VisibilityPreference.TO_BOTH:
            return is_invited_or_attending or is_same_org_member
        case _:
            return False
