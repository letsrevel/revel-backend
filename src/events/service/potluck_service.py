"""Service functions for potluck management."""

import typing as t
from uuid import UUID

import structlog
from django.db.models import Q

from accounts.models import RevelUser
from events.models import Event, EventRSVP, PotluckItem, Ticket

logger = structlog.get_logger(__name__)


def create_potluck_item(
    event: Event,
    created_by: RevelUser,
    *,
    assignee: RevelUser | None = None,
    **kwargs: t.Any,
) -> PotluckItem:
    """Create a new potluck item for an event.

    Args:
        event: The event this item belongs to
        created_by: User who created the item
        assignee: Optional user to immediately claim the item (atomic create+claim)
        **kwargs: Additional fields for the potluck item

    Returns:
        The created PotluckItem instance
    """
    return PotluckItem.objects.create(event=event, created_by=created_by, assignee=assignee, **kwargs)


def claim_potluck_item(potluck_item: PotluckItem, user: RevelUser) -> PotluckItem:
    """Claim a potluck item.

    Args:
        potluck_item: The item to claim
        user: User claiming the item

    Returns:
        The updated PotluckItem instance

    Raises:
        ValueError: If the item is already claimed by someone else
    """
    if potluck_item.assignee and potluck_item.assignee != user:
        raise ValueError("This item is already claimed by another user")

    potluck_item.assignee = user
    potluck_item.save(update_fields=["assignee"])
    return potluck_item


def unclaim_potluck_item(potluck_item: PotluckItem) -> PotluckItem:
    """Unclaim a potluck item.

    Args:
        potluck_item: The item to unclaim

    Returns:
        The updated PotluckItem instance
    """
    potluck_item.assignee = None
    potluck_item.save(update_fields=["assignee"])
    return potluck_item


def unclaim_user_potluck_items(event_id: UUID, user_id: UUID) -> int:
    """Unclaim all potluck items for a user at an event.

    This is called when a user's participation status changes (RSVP NO/MAYBE,
    ticket cancelled, or participation deleted).

    Args:
        event_id: The event ID
        user_id: The user ID whose items should be unclaimed

    Returns:
        Number of items that were unclaimed
    """
    unclaimed_count = PotluckItem.objects.filter(event_id=event_id, assignee_id=user_id).update(assignee=None)

    if unclaimed_count > 0:
        logger.info(
            "potluck_items_unclaimed",
            event_id=str(event_id),
            user_id=str(user_id),
            count=unclaimed_count,
        )

    return unclaimed_count


def user_may_hold_potluck_claims(event_id: UUID, user_id: UUID, *, any_ticket: bool = False) -> bool:
    """Whether the user is entitled to hold potluck claims for the event.

    The single definition shared by the permission (granting) and the signals (releasing):
    an attending ticket (ACTIVE or CHECKED_IN), a YES RSVP, or the organization's owner or
    staff (same semantics as ``Organization.is_owner_or_staff``).

    Args:
        event_id: The event ID.
        user_id: The user ID.
        any_ticket: Accept a ticket of any status (used when releasing an item the user
            already holds, e.g. after a cancellation).
    """
    tickets = Ticket.objects.all() if any_ticket else Ticket.objects.attending()
    return (
        tickets.filter(event_id=event_id, user_id=user_id).exists()
        or EventRSVP.objects.filter(event_id=event_id, user_id=user_id, status=EventRSVP.RsvpStatus.YES).exists()
        or Event.objects.filter(pk=event_id)
        .filter(Q(organization__owner_id=user_id) | Q(organization__staff_members=user_id))
        .exists()
    )


def release_potluck_claims_unless_entitled(event_id: UUID, user_id: UUID) -> None:
    """Unclaim the user's potluck items unless they may still hold claims.

    Cheap when the user holds no claim: one ``EXISTS`` and nothing else, which keeps
    cascade deletes and ordinary saves inexpensive.
    """
    if not PotluckItem.objects.filter(event_id=event_id, assignee_id=user_id).exists():
        return
    if not user_may_hold_potluck_claims(event_id, user_id):
        unclaim_user_potluck_items(event_id, user_id)
