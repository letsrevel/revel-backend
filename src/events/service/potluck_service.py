"""Service functions for potluck management."""

import typing as t
from uuid import UUID

import structlog

from accounts.models import RevelUser
from events.models import Event, PotluckItem

logger = structlog.get_logger(__name__)


def create_potluck_item(event: Event, created_by: RevelUser, **kwargs: t.Any) -> PotluckItem:
    """Create a new potluck item for an event.

    Args:
        event: The event this item belongs to
        created_by: User who created the item
        **kwargs: Additional fields for the potluck item

    Returns:
        The created PotluckItem instance
    """
    return PotluckItem.objects.create(event=event, created_by=created_by, **kwargs)


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
