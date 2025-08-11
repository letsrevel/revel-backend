import typing as t

from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.geos import Point
from ninja_extra import ControllerBase

from accounts.models import RevelUser


class UserAwareController(ControllerBase):
    def maybe_user(self) -> RevelUser | AnonymousUser:
        """Get the user for this request."""
        return t.cast(RevelUser | AnonymousUser, self.context.request.user)  # type: ignore[union-attr]

    def user(self) -> RevelUser:
        """Get the user for this request."""
        return t.cast(RevelUser, self.context.request.user)  # type: ignore[union-attr]

    def user_location(self) -> Point | None:
        """Get the user's location from the request."""
        return t.cast(Point | None, self.context.request.user_location.get())  # type: ignore[union-attr]
