from ninja_extra.throttling import AnonRateThrottle, UserRateThrottle


class AnonDefaultThrottle(AnonRateThrottle):
    rate = "60/min"


class UserDefaultThrottle(UserRateThrottle):
    rate = "100/min"


class AuthThrottle(AnonRateThrottle):
    rate = "100/min"


class MediaValidationThrottle(AnonRateThrottle):
    """Throttle for media validation endpoint called by Caddy.

    This is higher than AuthThrottle because:
    1. Caddy is a single IP making all validation requests
    2. A page may load multiple protected images simultaneously
    3. The endpoint is lightweight (HMAC verification only)

    The rate is set to 1000/min to handle burst loading scenarios
    while still providing brute-force protection.
    """

    rate = "1000/min"


class UserRegistrationThrottle(AnonRateThrottle):
    rate = "100/day"


class WriteThrottle(UserRateThrottle):
    rate = "100/min"


class GeoThrottle(AnonRateThrottle):
    rate = "100/min"


class QuestionnaireSubmissionThrottle(UserRateThrottle):
    rate = "100/min"


class UserRequestThrottle(UserRateThrottle):
    rate = "100/min"


class UserDataExportThrottle(UserRateThrottle):
    rate = "30/day"
