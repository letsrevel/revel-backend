"""Service for managing user location caching and retrieval."""

import typing as t

from django.contrib.gis.geos import Point
from django.core.cache import cache

from accounts.models import RevelUser


def get_user_location_cache_key(user_id: t.Any) -> str:
    """Generate cache key for user location.

    Args:
        user_id: The user ID (can be UUID, int, or str).

    Returns:
        str: Cache key for the user's location.
    """
    return f"user_location:{user_id}"


def invalidate_user_location_cache(user_id: t.Any) -> None:
    """Invalidate cached user location.

    Args:
        user_id: The user ID whose location cache should be invalidated.
    """
    cache.delete(get_user_location_cache_key(user_id))


def get_user_location_from_preferences(user: RevelUser) -> Point | None:
    """Get user's location from their saved city preference.

    Args:
        user: The user whose location to retrieve.

    Returns:
        Point | None: The user's city location, or None if not set.
    """
    from events.models import GeneralUserPreferences

    try:
        preferences = GeneralUserPreferences.objects.select_related("city").get(user=user)
        if preferences.city and preferences.city.location:
            return preferences.city.location
    except GeneralUserPreferences.DoesNotExist:
        pass

    return None


def get_cached_user_location(user: RevelUser, fallback_location: Point | None = None) -> Point | None:
    """Get user's location with caching, prioritizing saved city preference.

    Args:
        user: The user whose location to retrieve.
        fallback_location: Location to use if no preference is set (e.g., from IP detection).

    Returns:
        Point | None: The user's location (from preference or fallback), or None.
    """

    def _get_location() -> Point | None:
        """Callback to compute location when cache miss occurs."""
        # Try to get from user preferences first
        location = get_user_location_from_preferences(user)
        if location:
            return location

        # Fall back to provided fallback (e.g., IP-based detection)
        return fallback_location

    # Use cache.get_or_set with a 1-hour timeout (invalidated on preference change)
    return cache.get_or_set(get_user_location_cache_key(user.id), _get_location, timeout=3600)
