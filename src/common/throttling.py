from ninja_extra.throttling import AnonRateThrottle, UserRateThrottle


class AnonDefaultThrottle(AnonRateThrottle):
    rate = "60/min"


class UserDefaultThrottle(UserRateThrottle):
    rate = "100/min"


class AuthThrottle(AnonRateThrottle):
    rate = "100/min"


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
