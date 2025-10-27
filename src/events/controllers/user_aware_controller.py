import typing as t

from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.geos import Point
from ninja_extra import ControllerBase

from accounts.models import RevelUser
from events.service.location_service import get_cached_user_location


class UserAwareController(ControllerBase):
    def maybe_user(self) -> RevelUser | AnonymousUser:
        """Get the user for this request."""
        return t.cast(RevelUser | AnonymousUser, self.context.request.user)  # type: ignore[union-attr]

    def user(self) -> RevelUser:
        """Get the user for this request."""
        return t.cast(RevelUser, self.context.request.user)  # type: ignore[union-attr]

    def user_location(self) -> Point | None:
        """Get the user's location, prioritizing saved city preference over IP-based detection.

        The result is cached using Django's cache system and invalidated when preferences change.

        Returns:
            Point | None: User's location as a Point (from saved city preference if available,
                         otherwise from IP-based detection), or None if unavailable.
        """
        user = self.maybe_user()

        # Anonymous users always fall back to IP-based detection (not cached)
        if user.is_anonymous:
            return t.cast(Point | None, self.context.request.user_location.get())  # type: ignore[union-attr]

        # Get IP-based fallback location
        fallback_location = t.cast(Point | None, self.context.request.user_location.get())  # type: ignore[union-attr]

        # Get cached location (preference or fallback)
        return get_cached_user_location(user, fallback_location)
