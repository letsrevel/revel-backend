"""Public referral endpoints: code validation, program applications, invite lookup."""

import typing as t
from uuid import UUID

from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja import Query, Schema
from ninja.errors import HttpError
from ninja_extra import ControllerBase, api_controller, route

from accounts import schema
from accounts.models import ReferralApplication, ReferralCode
from accounts.service import referral_application_service
from common.schema import ResponseOk
from common.throttling import ReferralApplicationThrottle


class ReferralValidationResponse(Schema):
    valid: bool = True


@api_controller("/referral", tags=["Referral"], auth=None)
class ReferralController(ControllerBase):
    @route.get(
        "/validate",
        response=ReferralValidationResponse,
        url_name="validate-referral-code",
    )
    def validate(self, code: t.Annotated[str, Query(...)]) -> ReferralValidationResponse:
        """Validate a referral code.

        Returns 200 if the code exists and is active, 404 otherwise.
        No referrer identity is leaked.
        """
        if not ReferralCode.objects.filter(code__iexact=code, is_active=True).exists():
            raise HttpError(404, str(_("Invalid or inactive referral code.")))
        return ReferralValidationResponse()

    @route.post(
        "/apply",
        response={
            202: ResponseOk,
            404: schema.ReferralApplicationErrorSchema,
            409: schema.ReferralApplicationErrorSchema,
        },
        url_name="referral-apply",
        throttle=ReferralApplicationThrottle(),
    )
    def apply(self, payload: schema.ReferralApplicationSchema) -> tuple[int, ResponseOk]:
        """Apply to the referral program.

        Always 202 for blocked or already-enrolled emails (no enrollment probing);
        409 for a pending duplicate or a taken code; 404 while applications are closed.
        """
        referral_application_service.submit_application(email=payload.email, code=payload.code, note=payload.note)
        return 202, ResponseOk()

    @route.get(
        "/invitations/{application_id}",
        response=schema.ReferralInvitationSchema,
        url_name="referral-invitation",
    )
    def get_invitation(self, application_id: UUID) -> ReferralApplication:
        """Return the email and code of an approved, not-yet-enrolled invite (for the register page)."""
        return get_object_or_404(
            ReferralApplication,
            id=application_id,
            status=ReferralApplication.Status.APPROVED,
            user__isnull=True,
        )
