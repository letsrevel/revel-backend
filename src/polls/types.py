"""Shared type aliases for the polls app."""

from django.contrib.auth.models import AnonymousUser

from accounts.models import RevelUser

UserLike = RevelUser | AnonymousUser

__all__ = ["UserLike"]
