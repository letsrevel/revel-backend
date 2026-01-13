from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpRequest
from ninja_extra import NinjaExtraAPI

from accounts.controllers.account import AccountController
from accounts.controllers.auth import AuthController
from accounts.controllers.dietary import DietaryController
from accounts.controllers.otp import OtpController
from common.controllers import TagController
from common.models import Legal
from common.schema import LegalSchema, ResponseOk, VersionResponse
from common.throttling import AnonDefaultThrottle, UserDefaultThrottle
from events.controllers.dashboard import DashboardController
from events.controllers.event_admin import EVENT_ADMIN_CONTROLLERS
from events.controllers.event_series import EventSeriesController
from events.controllers.event_series_admin import EventSeriesAdminController
from events.controllers.events import EventController
from events.controllers.organization import OrganizationController
from events.controllers.organization_admin import ORGANIZATION_ADMIN_CONTROLLERS
from events.controllers.permissions import PermissionController
from events.controllers.potluck import PotluckController
from events.controllers.questionnaire import QuestionnaireController
from events.controllers.stripe_webhook import StripeWebhookController
from events.controllers.user_preferences import UserPreferencesController
from events.exceptions import AlreadyMemberError, PendingMembershipRequestExistsError, TooManyItemsError
from events.service.event_manager import UserIsIneligibleError
from geo.controllers.cities import CityController
from notifications.controllers.notification_controller import NotificationController
from notifications.controllers.preference_controller import NotificationPreferenceController
from questionnaires.exceptions import (
    CrossQuestionnaireSubmissionError,
    MissingMandatoryAnswerError,
    QuestionIntegrityError,
    SectionIntegrityError,
)
from telegram.controllers import TelegramController
from wallet.controllers import TicketWalletController

from .exception_handlers import (
    handle_already_member_error,
    handle_cross_questionnaire_submission_error,
    handle_django_validation_error,
    handle_general_exception,
    handle_missing_mandatory_answers_submission_error,
    handle_pending_membership_request_exists_error,
    handle_question_integrity_error,
    handle_section_integrity_error,
    handle_too_many_items_error,
    handle_user_is_ineligible_error,
)

api = NinjaExtraAPI(
    title="REVEL Backend API",
    docs_url="/docs",
    # docs_decorator=staff_member_required if not settings.DEBUG else None,
    version=settings.VERSION,
    description=f"Revel API {settings.VERSION}",
    app_name=f"revel-api-{settings.VERSION}",
    urls_namespace="api",
    servers=[
        {"url": settings.SERVICE_URL, "description": settings.SERVICE_DESCRIPTION},
    ],
    throttle=[AnonDefaultThrottle(), UserDefaultThrottle()],
)


@api.get("/version", tags=["Version"], response={200: VersionResponse})
def version(request: HttpRequest) -> tuple[int, VersionResponse]:
    """Get the API version.

    Args:
        request: The incoming HTTP request.

    Returns:
        The response status code and message.
    """
    return 200, VersionResponse(version=settings.VERSION, demo=settings.DEMO_MODE)


@api.get("/healthcheck", tags=["Healthcheck"], response={200: ResponseOk})
def healthcheck(request: HttpRequest) -> tuple[int, ResponseOk]:
    """Check the health of the API.

    Args:
        request: The incoming HTTP request.

    Returns:
        The response status code and message.
    """
    return 200, ResponseOk()


@api.get("/legal", tags=["Legal"], response={200: LegalSchema})
def legal(request: HttpRequest) -> tuple[int, Legal]:
    """Return legal documents."""
    return 200, Legal.get_solo()


api.register_controllers(
    # Auth/Account controllers
    AuthController,
    OtpController,
    AccountController,
    DietaryController,
    # Event controllers
    DashboardController,
    OrganizationController,
    *ORGANIZATION_ADMIN_CONTROLLERS,
    EventController,
    *EVENT_ADMIN_CONTROLLERS,
    PermissionController,
    EventSeriesController,
    EventSeriesAdminController,
    PotluckController,
    QuestionnaireController,
    UserPreferencesController,
    StripeWebhookController,
    # Common controllers
    TagController,
    # Geo controllers
    CityController,
    # Notification controllers
    NotificationController,
    NotificationPreferenceController,
    # Telegram controllers
    TelegramController,
    # Wallet controllers
    TicketWalletController,
)

EXCEPTION_HANDLERS = {
    Exception: handle_general_exception,
    ValidationError: handle_django_validation_error,
    UserIsIneligibleError: handle_user_is_ineligible_error,
    CrossQuestionnaireSubmissionError: handle_cross_questionnaire_submission_error,
    MissingMandatoryAnswerError: handle_missing_mandatory_answers_submission_error,
    SectionIntegrityError: handle_section_integrity_error,
    QuestionIntegrityError: handle_question_integrity_error,
    TooManyItemsError: handle_too_many_items_error,
    AlreadyMemberError: handle_already_member_error,
    PendingMembershipRequestExistsError: handle_pending_membership_request_exists_error,
}

for exc, handler in EXCEPTION_HANDLERS.items():
    api.add_exception_handler(exc, handler)
