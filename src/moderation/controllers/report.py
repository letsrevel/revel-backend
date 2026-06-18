from ninja_extra import api_controller, route, status

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.throttling import UserDefaultThrottle, WriteThrottle
from moderation import schema
from moderation.models import ContentReport
from moderation.service.report import create_content_report


@api_controller("/moderation", tags=["Moderation"], auth=I18nJWTAuth(), throttle=UserDefaultThrottle())
class ModerationController(UserAwareController):
    """User-facing content reporting."""

    @route.post(
        "/reports",
        response={201: schema.ReportSchema},
        url_name="create-content-report",
        throttle=WriteThrottle(),
    )
    def create_report(self, payload: schema.ReportCreateSchema) -> tuple[int, ContentReport]:
        """Report a piece of content as abusive/offensive. 404 if the target is not reportable."""
        report = create_content_report(reporter=self.user(), payload=payload)
        return status.HTTP_201_CREATED, report
