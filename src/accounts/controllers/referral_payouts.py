"""Referral payout and statement endpoints for authenticated users."""

from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from accounts import models, schema
from common.authentication import I18nJWTAuth
from common.controllers.base import UserAwareController
from common.signing import get_file_url
from common.throttling import UserDefaultThrottle


@api_controller(
    "/me/referral",
    auth=I18nJWTAuth(),
    tags=["User Referral Payouts"],
    throttle=UserDefaultThrottle(),
)
class ReferralPayoutController(UserAwareController):
    """Read-only endpoints for a user's referral payouts and statements."""

    def _get_statement(self, payout_id: UUID) -> models.ReferralPayoutStatement:
        """Fetch a payout's statement, scoped to the current user."""
        payout = get_object_or_404(
            models.ReferralPayout.objects.select_related("statement"),
            id=payout_id,
            referral__referrer=self.user(),
        )
        if not hasattr(payout, "statement"):
            raise HttpError(404, str(_("Statement not yet generated for this payout.")))
        return payout.statement

    @route.get(
        "/payouts",
        url_name="list_referral_payouts",
        response=PaginatedResponseSchema[schema.ReferralPayoutSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_payouts(self) -> QuerySet[models.ReferralPayout]:
        """List all referral payouts for the authenticated user."""
        return (
            models.ReferralPayout.objects.filter(referral__referrer=self.user())
            .select_related("statement")
            .order_by("-period_start", "-created_at")
        )

    @route.get(
        "/payouts/{payout_id}/statement",
        url_name="get_payout_statement",
        response=schema.ReferralPayoutStatementSchema,
    )
    def get_statement(self, payout_id: UUID) -> models.ReferralPayoutStatement:
        """Get the statement for a specific payout."""
        return self._get_statement(payout_id)

    @route.get(
        "/payouts/{payout_id}/statement/download",
        url_name="download_payout_statement",
        response=schema.StatementDownloadURLSchema,
    )
    def download_statement(self, payout_id: UUID) -> schema.StatementDownloadURLSchema:
        """Get a signed download URL for a payout statement PDF."""
        statement = self._get_statement(payout_id)
        url = get_file_url(statement.pdf_file)
        if not url:
            raise HttpError(404, str(_("Statement PDF not yet generated.")))
        return schema.StatementDownloadURLSchema(download_url=url)
