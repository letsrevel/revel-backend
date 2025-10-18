import typing as t
from uuid import UUID

from django.utils import timezone
from pydantic import BaseModel

from accounts.models import RevelUser
from events.models import (
    BaseUserPreferences,
    Event,
    EventInvitation,
    EventRSVP,
    GeneralUserPreferences,
    OrganizationMember,
    Ticket,
    UserEventPreferences,
    UserEventSeriesPreferences,
    UserOrganizationPreferences,
)
from events.service import update_db_instance
from events.tasks import build_attendee_visibility_flags

AnyPref = GeneralUserPreferences | UserEventPreferences | UserEventSeriesPreferences | UserOrganizationPreferences

T = t.TypeVar("T", bound=AnyPref)


def set_preferences(
    instance: T,
    payload: BaseModel,
    *,
    overwrite_children: bool = False,
) -> T:
    """Update preferences and optionally propagate to children."""
    updated_instance = update_db_instance(instance, payload, exclude_unset=False, exclude_defaults=False)
    visibility_changed = False

    if payload and "show_me_on_attendee_list" in payload.model_fields_set:
        old_value = getattr(instance, "show_me_on_attendee_list", None)
        new_value = getattr(updated_instance, "show_me_on_attendee_list", None)
        visibility_changed = old_value != new_value

    if not overwrite_children or isinstance(updated_instance, UserEventPreferences):
        if visibility_changed:
            trigger_visibility_flags_for_user(updated_instance.user.pk)
        return updated_instance
    return _cascade_updates(updated_instance, payload)


def _cascade_updates(updated_instance: T, payload: BaseModel) -> T:
    """Cascade update children preferences."""
    # --- BFS traversal to update children ---
    to_update: list[AnyPref] = [updated_instance]
    seen_events: set[UUID] = set()

    while to_update:
        current = to_update.pop()

        if isinstance(current, GeneralUserPreferences):
            to_update.extend(UserOrganizationPreferences.objects.filter(user=current.user))
        elif isinstance(current, UserOrganizationPreferences):
            to_update.extend(
                UserEventSeriesPreferences.objects.filter(
                    user=current.user, event_series__organization=current.organization
                )
            )
            to_update.extend(
                UserEventPreferences.objects.filter(user=current.user, event__organization=current.organization)
            )
        elif isinstance(current, UserEventSeriesPreferences):
            to_update.extend(
                UserEventPreferences.objects.filter(user=current.user, event__event_series=current.event_series)
            )
        elif isinstance(current, UserEventPreferences):
            if current.event_id:
                seen_events.add(current.event_id)

        # Re-apply the update to each child node
        updated = update_db_instance(current, payload)
        if payload and "show_me_on_attendee_list" in payload.model_fields_set:
            old = getattr(current, "show_me_on_attendee_list", None)
            new = getattr(updated, "show_me_on_attendee_list", None)
            if old != new and isinstance(current, UserEventPreferences):
                seen_events.add(current.event_id)

    for event_id in seen_events:
        build_attendee_visibility_flags.delay(str(event_id))

    return updated_instance


def trigger_visibility_flags_for_user(user_id: UUID) -> None:
    """Dispatch build_attendee_visibility_flags for all future events the user is attending."""
    event_ids = (
        Ticket.objects.filter(
            user_id=user_id,
            status=Ticket.Status.ACTIVE,
            event__start__gte=timezone.now(),
        )
        .values_list("event_id", flat=True)
        .union(
            EventRSVP.objects.filter(
                user_id=user_id,
                status=EventRSVP.Status.YES,
                event__start__gte=timezone.now(),
            ).values_list("event_id", flat=True)
        )
    )
    for event_id in event_ids:
        build_attendee_visibility_flags.delay(str(event_id))


def resolve_visibility(
    viewer: RevelUser, target: RevelUser, event: Event, owner_id: UUID, staff_ids: set[UUID]
) -> bool:
    """Resolves whether an attendee (target) is visible to another user (viewer)."""
    if viewer.id == owner_id or viewer.id in staff_ids:
        return True

    try:
        prefs = UserEventPreferences.objects.get(user=target, event=event)
    except UserEventPreferences.DoesNotExist:
        prefs = getattr(target, "general_preferences", None)  # type: ignore[assignment]

    if not prefs:
        return False

    visibility = prefs.show_me_on_attendee_list
    org_id = event.organization_id

    if visibility == BaseUserPreferences.VisibilityPreference.ALWAYS:
        return True

    if visibility == BaseUserPreferences.VisibilityPreference.NEVER:
        return False

    is_invited_or_attending = (
        EventInvitation.objects.filter(event=event, user=viewer).exists()
        or Ticket.objects.filter(event=event, user=viewer, status=Ticket.Status.ACTIVE).exists()
        or EventRSVP.objects.filter(event=event, user=viewer, status=EventRSVP.Status.YES).exists()
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
        case BaseUserPreferences.VisibilityPreference.TO_MEMBERS:
            return is_same_org_member
        case BaseUserPreferences.VisibilityPreference.TO_INVITEES:
            return is_invited_or_attending
        case BaseUserPreferences.VisibilityPreference.TO_BOTH:
            return is_invited_or_attending or is_same_org_member
        case _:
            return False
