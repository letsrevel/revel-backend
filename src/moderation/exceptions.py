class FoodItemBlockedError(Exception):
    """Raised when a food-item name matches the blocklist exactly (live hard-block)."""
