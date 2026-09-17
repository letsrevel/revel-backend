import functools
import typing as t

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
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


class ReferralApplicationThrottle(DisableableThrottleMixin, AnonRateThrottle):
    """Public referral-program applications (10 requests/day per IP)."""

    scope = "referral_application"
    rate = "10/day"


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


class _IPRateThrottle(DisableableThrottleMixin, AnonRateThrottle):
    """Throttle every caller by client IP, authenticated or not.

    ``AnonRateThrottle.get_cache_key`` returns ``None`` — and ninja's
    ``SimpleRateThrottle.allow_request`` then allows the request unconditionally — as soon as
    ``request.user.is_authenticated``. That is correct for a ninja controller, where the auth
    class runs and a ``UserRateThrottle`` takes over. It is NOT correct for DOT's protocol
    views: those are plain Django views sitting behind ``SessionMiddleware`` and
    ``AuthenticationMiddleware``, so a mere session cookie makes ``request.user``
    authenticated and would switch their rate limiting off entirely. Anyone can obtain such a
    cookie on the API origin (``GOOGLE_SSO_ALLOWABLE_DOMAINS = ["*"]`` with auto-created
    users), so the exemption is self-service.

    Unauthenticated OAuth clients are the norm on these endpoints and there is no per-user
    identity to key on before the token is issued, so the client IP is the only honest key.
    ``get_ident`` trusts exactly the last ``X-Forwarded-For`` entry (``NUM_PROXIES = 1``, the
    one Caddy appends), so the bucket cannot be evaded with a forged header.

    Deliberately a new base rather than a change to the classes above: every other endpoint in
    this module wants the stock anonymous-only semantics.
    """

    def get_cache_key(self, request: HttpRequest) -> str | None:
        """Key on the client IP, skipping the authenticated-user short-circuit."""
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class OAuthTokenThrottle(_IPRateThrottle):
    """OAuth token, revocation, userinfo and registration-management endpoints (60/min per IP)."""

    scope = "oauth_token"
    rate = "60/min"


class OAuthRegistrationThrottle(_IPRateThrottle):
    """RFC 7591 dynamic client registration (10/hour per IP)."""

    scope = "oauth_register"
    rate = "10/hour"


# ``BaseThrottle.wait()`` returns None when it cannot compute a delay (no recorded history,
# or ``rate`` unset). One minute is the shortest window any OAuth throttle above uses, so it
# is a safe floor for the RFC 6585 ``Retry-After`` hint rather than omitting the header.
# Only ``None`` falls back: a computed 0.0 is a legitimate "retry now".
_RETRY_AFTER_FALLBACK_SECONDS = 60


def throttled(
    throttle_cls: type[AnonRateThrottle],
) -> t.Callable[[t.Callable[..., HttpResponse]], t.Callable[..., HttpResponse]]:
    """Apply a ninja throttle to a plain Django view.

    DOT's protocol views live outside ninja, so ninja-extra's ``throttle=`` plumbing never
    sees them. The 429 body uses OAuth's own ``slow_down`` error code (RFC 8628 §3.5) rather
    than ninja's ``detail`` shape, because the callers are OAuth clients.

    Args:
        throttle_cls: The throttle class to instantiate per request.

    Returns:
        A view decorator.
    """

    def decorator(view: t.Callable[..., HttpResponse]) -> t.Callable[..., HttpResponse]:
        @functools.wraps(view)
        def wrapped(request: HttpRequest, *args: t.Any, **kwargs: t.Any) -> HttpResponse:
            throttle = throttle_cls()
            if not throttle.allow_request(request):
                wait = throttle.wait()
                response = JsonResponse({"error": "slow_down"}, status=429)
                response["Retry-After"] = str(int(_RETRY_AFTER_FALLBACK_SECONDS if wait is None else wait))
                return response
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
