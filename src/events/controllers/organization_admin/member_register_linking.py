"""Org-admin invitations that link register records to accounts (#1319)."""

from uuid import UUID

from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.schema import ErrorDetail
from common.throttling import MemberRegisterBulkInviteThrottle, UserDefaultThrottle, WriteThrottle
from events import models, schema
from events.controllers.permissions import OrganizationPermission
from events.service import member_register_linking, member_register_reconciliation

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

    @route.get(
        "/member-register/possible-duplicates",
        url_name="list_member_register_possible_duplicates",
        response=list[schema.PossibleDuplicateSchema],
        throttle=UserDefaultThrottle(),
    )
    def list_possible_duplicates(self, slug: str) -> list[member_register_reconciliation.PossibleDuplicate]:
        """Unlinked entries that may be the same person as a linked open entry (by email, name or collision claim)."""
        return member_register_reconciliation.possible_duplicates(self.get_one(slug))

    # Literal segments above must stay registered before the ``{record_id}`` routes below.
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

    @route.post(
        "/member-register/{record_id}/merge",
        url_name="merge_member_register_record",
        response={200: schema.MembershipRecordSchema, 400: ErrorDetail, 404: ErrorDetail},
    )
    def merge_member_register_record(
        self, slug: str, record_id: UUID, payload: schema.MemberRegisterMergeSchema
    ) -> models.MembershipRecord:
        """Merge an account-less entry into this one (an open duplicate is absorbed; a closed one is attached)."""
        organization = self.get_one(slug)
        survivor = get_object_or_404(models.MembershipRecord, pk=record_id, organization=organization)
        absorbed = get_object_or_404(models.MembershipRecord, pk=payload.absorbed_record_id, organization=organization)
        return member_register_reconciliation.merge_records(survivor, absorbed)

    @route.post(
        "/member-register/{record_id}/unlink",
        url_name="unlink_member_register_record",
        response={200: schema.MembershipRecordSchema, 400: ErrorDetail, 404: ErrorDetail, 409: ErrorDetail},
    )
    def unlink_member_register_record(self, slug: str, record_id: UUID) -> models.MembershipRecord:
        """Reverse a link or a merge. Reversing the link of an open entry cancels the access it granted."""
        organization = self.get_one(slug)
        record = get_object_or_404(models.MembershipRecord, pk=record_id, organization=organization)
        return member_register_reconciliation.unlink_record(record)

    @route.get(
        "/membership-requests/{request_id}/register-matches",
        url_name="list_membership_request_register_matches",
        response={200: list[schema.RegisterMatchSchema], 404: ErrorDetail},
        throttle=UserDefaultThrottle(),
    )
    def list_membership_request_register_matches(
        self, slug: str, request_id: UUID
    ) -> list[member_register_reconciliation.RegisterMatch]:
        """Unlinked register entries that may be this applicant (verify, then approve and link)."""
        organization = self.get_one(slug)
        membership_request = get_object_or_404(
            models.OrganizationMembershipRequest.objects.select_related("user"),
            pk=request_id,
            organization=organization,
        )
        return member_register_reconciliation.register_matches_for_request(membership_request)
