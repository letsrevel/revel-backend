"""Org-admin invitations that link register records to accounts (#1319)."""

from uuid import UUID

from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.schema import ErrorDetail
from common.throttling import MemberRegisterBulkInviteThrottle, WriteThrottle
from events import models, schema
from events.controllers.permissions import OrganizationPermission
from events.service import member_register_linking

from .base import OrganizationAdminBaseController


@api_controller(
    "/organization-admin/{slug}",
    # Session-only, like the register itself (see OrganizationAdminMemberRegisterController).
    auth=I18nJWTAuth(),
    tags=["Organization Admin"],
    throttle=WriteThrottle(),
    permissions=[OrganizationPermission("manage_members")],
)
class OrganizationAdminMemberRegisterLinkingController(OrganizationAdminBaseController):
    """Invite people on the register to link their entry to an account."""

    @route.post(
        "/member-register/invite-all",
        url_name="invite_all_member_register",
        response={202: schema.MemberRegisterBulkInviteResultSchema},
        throttle=MemberRegisterBulkInviteThrottle(),
    )
    def invite_all_member_register(self, slug: str) -> tuple[int, schema.MemberRegisterBulkInviteResultSchema]:
        """Invite every open, unlinked, never-invited entry that has an email address."""
        organization = self.get_one(slug)
        count = member_register_linking.send_bulk_invites(organization)
        return 202, schema.MemberRegisterBulkInviteResultSchema(invited=count)

    @route.post(
        "/member-register/{record_id}/invite",
        url_name="invite_member_register_record",
        response={200: schema.MembershipRecordSchema, 400: ErrorDetail, 404: ErrorDetail},
    )
    def invite_member_register_record(self, slug: str, record_id: UUID) -> models.MembershipRecord:
        """Send (or resend) the claim link for one entry. A resend invalidates the previous link."""
        organization = self.get_one(slug)
        record = get_object_or_404(
            models.MembershipRecord.objects.select_related("tier"), pk=record_id, organization=organization
        )
        return member_register_linking.send_invite(record)
