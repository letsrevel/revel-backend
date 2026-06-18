"""Guard for screening food-item names against the blocklist."""

from moderation.blocklist.screen import is_blocked
from moderation.exceptions import FoodItemBlockedError


def screen_food_item_name(name: str) -> None:
    """Raise FoodItemBlockedError (→ 422) if the name exactly matches the blocklist."""
    if is_blocked(name):
        raise FoodItemBlockedError
