"""Public claim flow for register invitation links (#1319)."""

import typing as t

from django.utils.translation import gettext_lazy as _
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.schema import ErrorDetail
from common.throttling import MemberRegisterInviteLookupThrottle, WriteThrottle
from events import schema
from events.service import member_register_linking
from events.service.member_register_linking import LinkOutcome

ClaimErrorCode = t.Literal["already_member", "not_started", "banned"]

# Failure outcomes as response values, not ``HttpError``: ninja rolls the request transaction back on an
# exception, which would discard the ``duplicate_claimant`` flag staff need.
_FAILURES: dict[LinkOutcome, tuple[int, ClaimErrorCode, t.Any]] = {
    LinkOutcome.DUPLICATE: (
        409,
        "already_member",
        _(
            "You already have a membership in this organization. "
            "Its staff can review the duplicate entry in their member register."
        ),
    ),
    LinkOutcome.NOT_STARTED: (
        409,
        "not_started",
        _("This membership hasn't started yet. Try the link again once it has."),
    ),
    LinkOutcome.BANNED: (403, "banned", _("You can't join this organization.")),
}
_NOT_FOUND = _("Invitation not found.")


@api_controller("/member-register-invites", auth=I18nJWTAuth(), tags=["Member Register Invites"])
class MemberRegisterInvitesController(UserAwareController):
    """Preview and claim the link in a register invitation email. Session-only auth (no app tokens)."""

    @route.get(
        "/{token}",
        url_name="preview_member_register_invite",
        response={200: schema.MemberRegisterInvitePreviewSchema, 404: ErrorDetail},
        auth=None,
        throttle=MemberRegisterInviteLookupThrottle(),
    )
    def preview_invite(self, token: str) -> tuple[int, schema.MemberRegisterInvitePreviewSchema | ErrorDetail]:
        """Show which organization and entry an invitation link is for. 404 if unknown or already used."""
        record = member_register_linking.get_record_for_invite_token(token)
        if record is None:
            return 404, ErrorDetail(detail=str(_NOT_FOUND))
        return 200, schema.MemberRegisterInvitePreviewSchema(
            organization_name=record.organization.name,
            organization_slug=record.organization.slug,
            full_name=record.full_name,
        )

    @route.post(
        "/{token}/claim",
        url_name="claim_member_register_invite",
        response={
            200: schema.MemberRegisterInviteClaimSchema,
            403: schema.MemberRegisterInviteClaimErrorSchema,
            404: ErrorDetail,
            409: schema.MemberRegisterInviteClaimErrorSchema,
        },
        throttle=WriteThrottle(),
    )
    def claim_invite(
        self, token: str
    ) -> tuple[int, schema.MemberRegisterInviteClaimSchema | schema.MemberRegisterInviteClaimErrorSchema | ErrorDetail]:
        """Link the invited register entry to the logged-in account, whatever its email."""
        result = member_register_linking.claim_invite(token, self.user())
        if result is None:
            return 404, ErrorDetail(detail=str(_NOT_FOUND))
        if result.outcome in _FAILURES:
            status, code, message = _FAILURES[result.outcome]
            return status, schema.MemberRegisterInviteClaimErrorSchema(detail=str(message), code=code)
        return 200, schema.MemberRegisterInviteClaimSchema(
            organization_slug=result.record.organization.slug, record_id=result.record.pk
        )
