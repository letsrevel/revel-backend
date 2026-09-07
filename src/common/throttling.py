import typing as t

from django.conf import settings
from django.http import HttpRequest
from ninja_extra.throttling import AnonRateThrottle, UserRateThrottle

# Every throttle below MUST declare its own ``scope``. ninja's throttles key their
# request history in the cache as ``throttle_<scope>_<ident>``, and the base classes
# default to ``scope = "user"`` / ``"anon"``. Without an override, a 25/day throttle
# reads and writes the same bucket as the 100/min default throttle: the short-window
# throttles keep the list trimmed to the last minute, so the long-window limit is
# applied to "requests in the last minute" instead (#936). Only the two default
# throttles keep the base scope, matching ``NINJA_EXTRA["THROTTLE_RATES"]``.


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

    scope = "auth"
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

    scope = "media_validation"
    rate = "1000/min"


class UserRegistrationThrottle(DisableableThrottleMixin, AnonRateThrottle):
    """User registration throttle (100 requests/day)."""

    scope = "user_registration"
    rate = "100/day"


class WriteThrottle(DisableableThrottleMixin, UserRateThrottle):
    """Write operation throttle (100 requests/min)."""

    scope = "write"
    rate = "100/min"


class GeoThrottle(DisableableThrottleMixin, AnonRateThrottle):
    """Geolocation endpoint throttle (100 requests/min)."""

    scope = "geo"
    rate = "100/min"


class QuestionnaireSubmissionThrottle(DisableableThrottleMixin, UserRateThrottle):
    """Questionnaire submission throttle (100 requests/min)."""

    scope = "questionnaire_submission"
    rate = "100/min"


class UserRequestThrottle(DisableableThrottleMixin, UserRateThrottle):
    """User request throttle (100 requests/min)."""

    scope = "user_request"
    rate = "100/min"


class UserDataExportThrottle(DisableableThrottleMixin, UserRateThrottle):
    """User data export throttle (30 requests/day)."""

    scope = "user_data_export"
    rate = "30/day"


class ExportThrottle(DisableableThrottleMixin, UserRateThrottle):
    """Export generation throttle (1 request/min).

    Exports trigger expensive Celery tasks that generate XLSX files,
    so we limit to 1 per minute per user.
    """

    scope = "export"
    rate = "1/min"


class SendAnnouncementThrottle(DisableableThrottleMixin, UserRateThrottle):
    """Send announcement throttle (25 requests/day).

    Limits how many announcements can be sent per day to prevent
    notification spam to organization members.
    """

    scope = "send_announcement"
    rate = "25/day"
