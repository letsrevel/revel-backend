"""Org-admin personal membership invitations: the account becomes a member only by accepting."""

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from accounts.models import RevelUser
from common.authentication import I18nJWTAuth
from common.schema import ErrorDetail
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import models, schema
from events.controllers.permissions import OrganizationPermission
from events.service.organization_service import member_invitations

from .base import OrganizationAdminBaseController


@api_controller(
    "/organization-admin/{slug}",
    # Session-only, like the account lookup these invitations follow from.
    auth=I18nJWTAuth(),
    tags=["Organization Admin"],
    permissions=[OrganizationPermission("manage_members")],
)
class OrganizationAdminMemberInvitationsController(OrganizationAdminBaseController):
    """Invite an account to become a member; it joins once it accepts."""

    @route.get(
        "/member-invitations",
        url_name="list_member_invitations",
        response=PaginatedResponseSchema[schema.MemberInvitationSchema],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_member_invitations(self, slug: str) -> QuerySet[models.OrganizationToken]:
        """List invitations that were sent and not yet accepted or expired."""
        return member_invitations.list_pending_invitations(self.get_one(slug))

    @route.post(
        "/member-invitations",
        url_name="create_member_invitation",
        response={201: schema.MemberInvitationSchema, 400: schema.AlreadyMemberErrorSchema | ErrorDetail},
        throttle=WriteThrottle(),
    )
    def create_member_invitation(
        self, slug: str, payload: schema.MemberInvitationCreateSchema
    ) -> tuple[int, models.OrganizationToken]:
        """Invite an account to join at the given tier (re-inviting refreshes the same invitation)."""
        organization = self.get_one(slug)
        invitee = get_object_or_404(RevelUser, pk=payload.user_id, is_active=True, guest=False)
        tier = get_object_or_404(
            models.MembershipTier.objects.standard(), pk=payload.tier_id, organization=organization
        )
        invitation = member_invitations.invite_member(organization, invitee, tier, self.user())
        return 201, invitation

    @route.delete(
        "/member-invitations/{invitation_id}",
        url_name="revoke_member_invitation",
        response={204: None},
        throttle=WriteThrottle(),
    )
    def revoke_member_invitation(self, slug: str, invitation_id: str) -> tuple[int, None]:
        """Revoke a pending invitation."""
        member_invitations.revoke_invitation(self.get_one(slug), invitation_id)
        return 204, None
