from ninja_extra.throttling import AnonRateThrottle, UserRateThrottle


class AnonDefaultThrottle(AnonRateThrottle):
    rate = "60/min"


class UserDefaultThrottle(UserRateThrottle):
    rate = "100/min"


class AuthThrottle(AnonRateThrottle):
    rate = "5/min"


class WriteThrottle(UserRateThrottle):
    rate = "50/min"


class GeoThrottle(AnonRateThrottle):
    rate = "100/min"


class QuestionnaireSubmissionThrottle(UserRateThrottle):
    rate = "5/min"


class UserRequestThrottle(UserRateThrottle):
    rate = "5/min"


class UserDataExportThrottle(UserRateThrottle):
    rate = "3/day"
