"""Service for potluck related operations."""

from accounts.models import RevelUser
from events.exceptions import TooManyItemsError
from events.models import Event, PotluckItem

MAX_ITEMS = 100


def create_potluck_item(
    event: Event, name: str, item_type: str, created_by: RevelUser, quantity: str | None = None, note: str | None = None
) -> PotluckItem:
    """Create a potluck item."""
    if PotluckItem.objects.filter(event=event).count() >= MAX_ITEMS:
        raise TooManyItemsError
    return PotluckItem.objects.create(
        event=event,
        name=name,
        item_type=item_type,
        created_by=created_by,
        quantity=quantity,
        note=note,
        assignee=created_by,
    )


def claim_potluck_item(potluck_item: PotluckItem, user: RevelUser) -> PotluckItem:
    """Claim a potluck item."""
    potluck_item.assignee = user
    potluck_item.save()
    return potluck_item


def unclaim_potluck_item(potluck_item: PotluckItem) -> PotluckItem:
    """Unclaim a potluck item."""
    potluck_item.assignee = None
    potluck_item.save()
    return potluck_item
