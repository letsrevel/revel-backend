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


# Cached when the user has no saved city preference: ``cache.get_or_set`` cannot
# distinguish a cached ``None`` from a miss, and we still want to skip the
# preferences query for users without one.
_NO_PREFERENCE: t.Final[str] = "NO_PREFERENCE"


def get_cached_user_location(user: RevelUser, fallback_location: Point | None = None) -> Point | None:
    """Get user's location, prioritizing their saved city preference.

    Only the preference lookup is cached: it is stable and explicitly
    invalidated when preferences change (see ``invalidate_user_location_cache``).
    The fallback — typically the per-request IP-derived location — is volatile
    and essentially free to compute (in-process IP2Location lookup), so it is
    deliberately NEVER cached: freezing it for the TTL pins users to a stale
    location (a VPN, hotel, or proxy IP) for up to an hour.

    Args:
        user: The user whose location to retrieve.
        fallback_location: Location to use if no preference is set (e.g., from IP detection).

    Returns:
        Point | None: The user's location (from preference or fallback), or None.
    """
    preference = cache.get_or_set(
        get_user_location_cache_key(user.id),
        lambda: get_user_location_from_preferences(user) or _NO_PREFERENCE,
        timeout=3600,
    )
    if isinstance(preference, Point):
        return preference
    return fallback_location
