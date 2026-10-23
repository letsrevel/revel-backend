"""Base authentication classes for Revel API."""

import typing as t

import structlog
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from ninja_extra import status
from ninja_extra.exceptions import APIException
from ninja_jwt.authentication import JWTAuth
from ninja_jwt.exceptions import InvalidToken

SessionValidator = t.Callable[[t.Mapping[str, t.Any]], bool]

# Checks every access token's claims must pass. Apps register theirs from ``AppConfig.ready`` (as they do
# their exception handlers), so ``common`` stays app-agnostic.
_session_validators: list[SessionValidator] = []


def register_session_validator(validator: SessionValidator) -> None:
    """Register a check that every access token's claims must pass (idempotent)."""
    if validator not in _session_validators:
        _session_validators.append(validator)


class SessionRevoked(InvalidToken):
    """401 for an access token whose session a registered validator refused.

    A dedicated subclass so ``OptionalAuth`` can treat exactly this case as anonymous, while
    expired or forged tokens keep failing as before.
    """


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

    def get_user(self, validated_token: t.Any) -> t.Any:
        """Resolve the token's user, refusing a session any registered validator rejects.

        Runs on every authenticated request (e.g. ``accounts`` revokes sessions bound to a custom
        domain that stopped serving).

        Raises:
            SessionRevoked: A registered session validator refused the token's claims.
        """
        if not all(validator(validated_token.payload) for validator in _session_validators):
            raise SessionRevoked
        return super().get_user(validated_token)

    def authenticate(self, request: HttpRequest, token: str) -> t.Any:
        """Authenticate and verify user permissions.

        Args:
            request: The HTTP request object
            token: The JWT token string

        Returns:
            The authenticated user object if all checks pass

        Raises:
            PermissionDenied: If user doesn't meet required criteria
            SessionRevoked: If a registered session validator refused the token
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

            # Bind the user to the structlog request context. JWT auth runs at
            # the view layer, *after* StructlogContextMiddleware has bound the
            # request context (where request.user is still anonymous), so this
            # is the earliest point the user is known for API requests. The
            # middleware clears contextvars at the end of every request.
            if settings.FEATURE_OBSERVABILITY:
                structlog.contextvars.bind_contextvars(user_id=str(user.pk))

        return user
