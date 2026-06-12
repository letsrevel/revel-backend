from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpRequest
from ninja_extra import NinjaExtraAPI

from accounts.controllers.account import AccountController
from accounts.controllers.auth import AuthController
from accounts.controllers.billing import UserBillingController
from accounts.controllers.dietary import DietaryController
from accounts.controllers.otp import OtpController
from accounts.controllers.referral import ReferralController
from accounts.controllers.referral_payouts import ReferralPayoutController
from accounts.controllers.referral_stripe import ReferralStripeController
from common.controllers import MediaValidationController, TagController
from common.exception_handlers import ExceptionHandler, register_handlers
from common.models import Legal, SiteSettings
from common.schema import BannerSchema, LegalSchema, ResponseOk, VersionResponse
from common.throttling import AnonDefaultThrottle, UserDefaultThrottle
from events.controllers.dashboard import DashboardController
from events.controllers.event_admin import EVENT_ADMIN_CONTROLLERS
from events.controllers.event_public import EVENT_PUBLIC_CONTROLLERS
from events.controllers.event_series import EventSeriesController
from events.controllers.event_series_admin import EventSeriesAdminController
from events.controllers.exports import ExportController
from events.controllers.following import FollowingController
from events.controllers.me_applications import MeMembershipApplicationsController
from events.controllers.me_subscriptions import MeSubscriptionsController
from events.controllers.organization import OrganizationController
from events.controllers.organization_admin import ORGANIZATION_ADMIN_CONTROLLERS
from events.controllers.permissions import PermissionController
from events.controllers.potluck import PotluckController
from events.controllers.questionnaire import QuestionnaireController
from events.controllers.stripe_webhook import StripeWebhookController
from events.controllers.user_preferences import UserPreferencesController
from geo.controllers.cities import CityController
from notifications.controllers.notification_controller import NotificationController
from notifications.controllers.preference_controller import NotificationPreferenceController
from polls.controllers import POLL_CONTROLLERS
from questionnaires.controllers import QuestionnaireFileController
from telegram.controllers import TelegramController
from wallet.controllers import TicketWalletController

from .exception_handlers import handle_django_validation_error, handle_general_exception

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
    """Get the API version and optional maintenance banner.

    Args:
        request: The incoming HTTP request.

    Returns:
        The response status code and message.
    """
    banner = _get_active_banner()
    return 200, VersionResponse(version=settings.VERSION, demo=settings.DEMO_MODE, banner=banner)


def _get_active_banner() -> BannerSchema | None:
    """Return the maintenance banner if active, None otherwise."""
    site = SiteSettings.get_solo()
    if not site.is_maintenance_banner_active:
        return None
    return BannerSchema(
        message=site.maintenance_message,
        severity=SiteSettings.BannerSeverity(site.maintenance_severity),
        scheduled_at=site.maintenance_scheduled_at,
        ends_at=site.maintenance_ends_at,
    )


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
    ReferralController,
    ReferralPayoutController,
    ReferralStripeController,
    UserBillingController,
    # Event controllers
    DashboardController,
    OrganizationController,
    *ORGANIZATION_ADMIN_CONTROLLERS,
    *EVENT_PUBLIC_CONTROLLERS,
    *EVENT_ADMIN_CONTROLLERS,
    PermissionController,
    EventSeriesController,
    EventSeriesAdminController,
    PotluckController,
    QuestionnaireController,
    QuestionnaireFileController,
    UserPreferencesController,
    FollowingController,
    MeSubscriptionsController,
    MeMembershipApplicationsController,
    StripeWebhookController,
    ExportController,
    # Common controllers
    MediaValidationController,
    TagController,
    # Geo controllers
    CityController,
    # Notification controllers
    NotificationController,
    NotificationPreferenceController,
    # Poll controllers
    *POLL_CONTROLLERS,
    # Telegram controllers
    TelegramController,
    # Wallet controllers
    TicketWalletController,
)

# Only truly global handlers live here. App-specific exceptions self-register
# from each app's ``exception_handlers.py`` via its ``AppConfig.ready`` hook, and
# take precedence over the generic ``ValidationError`` fallback by MRO.
EXCEPTION_HANDLERS: dict[type[Exception], ExceptionHandler] = {
    Exception: handle_general_exception,
    ValidationError: handle_django_validation_error,
}

register_handlers(api, EXCEPTION_HANDLERS)
