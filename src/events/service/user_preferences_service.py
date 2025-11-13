"""Service for managing user preferences."""

from uuid import UUID

from django.utils import timezone
from pydantic import BaseModel

from accounts.models import RevelUser
from events.models import Event, EventInvitation, EventRSVP, GeneralUserPreferences, OrganizationMember, Ticket
from events.service import update_db_instance
from events.service.location_service import invalidate_user_location_cache
from events.tasks import build_attendee_visibility_flags


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


def resolve_visibility(
    viewer: RevelUser, target: RevelUser, event: Event, owner_id: UUID, staff_ids: set[UUID]
) -> bool:
    """Resolve whether an attendee (target) is visible to another user (viewer).

    Args:
        viewer: The user viewing the attendee list
        target: The target attendee being viewed
        event: The event context
        owner_id: The organization owner ID
        staff_ids: Set of organization staff IDs

    Returns:
        True if target should be visible to viewer
    """
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

    # For conditional visibility, check viewer's relationship
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
