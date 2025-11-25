"""Base authentication classes for Revel API."""

import typing as t

from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from ninja_extra import status
from ninja_extra.exceptions import APIException
from ninja_jwt.authentication import JWTAuth


class PermissionDenied(APIException):
    """Exception raised when user doesn't have required permissions."""

    status_code = status.HTTP_403_FORBIDDEN
    default_detail = _("Permission denied")


class BaseJWTAuth(JWTAuth):
    """Base JWT authentication with customizable permission checking.

    This base class allows for checking additional user permissions beyond
    simple authentication, such as requiring verified email, staff status,
    or superuser status.
    """

    def __init__(
        self,
        *,
        is_superuser: bool = False,
        is_staff: bool = False,
        requires_verified_email: bool = False,
    ) -> None:
        """Initialize the BaseJWTAuth authentication class.

        Args:
            is_superuser: Whether the user must be a Django superuser.
            is_staff: Whether the user must be a Django staff member.
            requires_verified_email: Whether the endpoint requires a verified email address.
        """
        self.is_superuser = is_superuser
        self.is_staff = is_staff
        self.requires_verified_email = requires_verified_email
        super().__init__()

    def authenticate(self, request: HttpRequest, token: str) -> t.Any:
        """Authenticate and verify user permissions.

        Args:
            request: The HTTP request object
            token: The JWT token string

        Returns:
            The authenticated user object if all checks pass

        Raises:
            PermissionDenied: If user doesn't meet required criteria
        """
        user = super().authenticate(request, token)

        if user and not isinstance(user, AnonymousUser):
            # Check email verification
            if self.requires_verified_email and not getattr(user, "email_verified", False):
                raise PermissionDenied(str(_("Email verification required.")))

            # Check staff status
            if self.is_staff and not getattr(user, "is_staff", False):
                raise PermissionDenied(str(_("Staff access required.")))

            # Check superuser status
            if self.is_superuser and not getattr(user, "is_superuser", False):
                raise PermissionDenied(str(_("Superuser access required.")))

        return user
