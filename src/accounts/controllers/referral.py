"""Controller for referral code validation."""

from django.utils.translation import gettext_lazy as _
from ninja import Query, Schema
from ninja.errors import HttpError
from ninja_extra import ControllerBase, api_controller, route

from accounts.models import ReferralCode


class ReferralValidationResponse(Schema):
    valid: bool = True


@api_controller("/referral", tags=["Referral"], auth=None)
class ReferralController(ControllerBase):
    @route.get(
        "/validate",
        response=ReferralValidationResponse,
        url_name="validate-referral-code",
    )
    def validate(self, code: str = Query(...)) -> ReferralValidationResponse:  # type: ignore[type-arg]
        """Validate a referral code.

        Returns 200 if the code exists and is active, 404 otherwise.
        No referrer identity is leaked.
        """
        if not ReferralCode.objects.filter(code=code.upper(), is_active=True).exists():
            raise HttpError(404, str(_("Invalid or inactive referral code.")))
        return ReferralValidationResponse()
