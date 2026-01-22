import typing as t

from django.conf import settings
from django.http import HttpRequest
from ninja_extra.throttling import AnonRateThrottle, UserRateThrottle


class DisableableThrottleMixin:
    """Mixin that allows throttling to be disabled via settings.

    When settings.DISABLE_THROTTLING is True, all throttle checks pass.
    Useful for load testing environments.
    """

    def allow_request(self, request: HttpRequest) -> bool:
        """Check if the request should be allowed.

        Returns True immediately if throttling is disabled, otherwise
        delegates to the parent throttle class.
        """
        if settings.DISABLE_THROTTLING:
            return True
        return t.cast(bool, super().allow_request(request))  # type: ignore[misc]


class AnonDefaultThrottle(DisableableThrottleMixin, AnonRateThrottle):
    """Anonymous user default throttle (60 requests/min)."""

    rate = "60/min"


class UserDefaultThrottle(DisableableThrottleMixin, UserRateThrottle):
    """Authenticated user default throttle (100 requests/min)."""

    rate = "100/min"


class AuthThrottle(DisableableThrottleMixin, AnonRateThrottle):
    """Authentication endpoint throttle (100 requests/min)."""

    rate = "100/min"


class MediaValidationThrottle(DisableableThrottleMixin, AnonRateThrottle):
    """Throttle for media validation endpoint called by Caddy.

    This is higher than AuthThrottle because:
    1. Caddy is a single IP making all validation requests
    2. A page may load multiple protected images simultaneously
    3. The endpoint is lightweight (HMAC verification only)

    The rate is set to 1000/min to handle burst loading scenarios
    while still providing brute-force protection.
    """

    rate = "1000/min"


class UserRegistrationThrottle(DisableableThrottleMixin, AnonRateThrottle):
    """User registration throttle (100 requests/day)."""

    rate = "100/day"


class WriteThrottle(DisableableThrottleMixin, UserRateThrottle):
    """Write operation throttle (100 requests/min)."""

    rate = "100/min"


class GeoThrottle(DisableableThrottleMixin, AnonRateThrottle):
    """Geolocation endpoint throttle (100 requests/min)."""

    rate = "100/min"


class QuestionnaireSubmissionThrottle(DisableableThrottleMixin, UserRateThrottle):
    """Questionnaire submission throttle (100 requests/min)."""

    rate = "100/min"


class UserRequestThrottle(DisableableThrottleMixin, UserRateThrottle):
    """User request throttle (100 requests/min)."""

    rate = "100/min"


class UserDataExportThrottle(DisableableThrottleMixin, UserRateThrottle):
    """User data export throttle (30 requests/day)."""

    rate = "30/day"
