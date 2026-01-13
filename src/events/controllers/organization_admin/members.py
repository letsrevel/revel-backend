from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja import Query
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from accounts.models import RevelUser
from common.authentication import I18nJWTAuth
from common.models import Tag
from common.schema import TagSchema
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import filters, models, schema
from events.controllers.permissions import IsOrganizationStaff, OrganizationPermission
from events.service import organization_service, update_db_instance

from .base import OrganizationAdminBaseController


@api_controller("/organization-admin/{slug}", auth=I18nJWTAuth(), tags=["Organization Admin"], throttle=WriteThrottle())
class OrganizationAdminMembersController(OrganizationAdminBaseController):
    """Organization membership management endpoints.

    Handles members, membership tiers, staff, and tags.
    """

    # ---- Members ----

    @route.get(
        "/members",
        url_name="list_organization_members",
        response=PaginatedResponseSchema[schema.OrganizationMemberSchema],
        permissions=[OrganizationPermission("manage_members")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(
        Searching,
        search_fields=["user__username", "user__email", "user__first_name", "user__last_name", "user__preferred_name"],
    )
    def list_members(
        self,
        slug: str,
        params: filters.MembershipFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.OrganizationMember]:
        """List all members of an organization."""
        organization = self.get_one(slug)
        qs = models.OrganizationMember.objects.filter(organization=organization).select_related("user", "tier")
        return params.filter(qs).distinct()

    @route.put(
        "/members/{user_id}",
        url_name="update_organization_member",
        response=schema.OrganizationMemberSchema,
        permissions=[OrganizationPermission("manage_members")],
    )
    def update_member(
        self, slug: str, user_id: UUID, payload: schema.OrganizationMemberUpdateSchema
    ) -> models.OrganizationMember:
        """Update a member's status and/or tier.

        Allows organization admins to:
        - Change membership status (active, paused, cancelled, banned)
        - Assign or change membership tier
        - Remove tier assignment by setting tier_id to null

        The tier must belong to the same organization as the membership.
        """
        organization = self.get_one(slug)
        member = get_object_or_404(
            models.OrganizationMember.objects.select_related("user", "tier"),
            organization=organization,
            user_id=user_id,
        )

        update_data = payload.model_dump(exclude_unset=True)
        if not update_data:
            return member

        # Resolve tier_id to tier object if provided
        tier = None
        clear_tier = False
        if "tier_id" in update_data:
            tier_id = update_data.pop("tier_id")
            if tier_id is None:
                clear_tier = True
            else:
                tier = get_object_or_404(models.MembershipTier, pk=tier_id, organization=organization)

        return organization_service.update_member(
            member, status=update_data.get("status"), tier=tier, clear_tier=clear_tier
        )

    @route.delete(
        "/members/{user_id}",
        url_name="remove_organization_member",
        response={204: None},
        permissions=[OrganizationPermission("manage_members")],
    )
    def remove_member(self, slug: str, user_id: UUID) -> tuple[int, None]:
        """Remove a member from an organization."""
        organization = self.get_one(slug)
        user_to_remove = get_object_or_404(RevelUser, id=user_id)
        organization_service.remove_member(organization, user_to_remove)
        return 204, None

    @route.post(
        "/members/{user_id}",
        url_name="create_organization_member",
        response={201: schema.OrganizationMemberSchema},
        permissions=[OrganizationPermission("manage_members")],
    )
    def add_member(
        self, slug: str, user_id: UUID, payload: schema.MemberAddSchema
    ) -> tuple[int, models.OrganizationMember]:
        """Add a member to an organization."""
        organization = self.get_one(slug)
        user_to_add = get_object_or_404(RevelUser, id=user_id)
        tier = get_object_or_404(models.MembershipTier, pk=payload.tier_id, organization=organization)
        return 201, organization_service.add_member(organization, user_to_add, tier)

    # ---- Membership Tiers ----

    @route.get(
        "/membership-tiers",
        url_name="list_membership_tiers",
        response=list[schema.MembershipTierSchema],
        permissions=[IsOrganizationStaff()],
        throttle=UserDefaultThrottle(),
    )
    def list_membership_tiers(self, slug: str) -> QuerySet[models.MembershipTier]:
        """List all membership tiers for an organization."""
        organization = self.get_one(slug)
        return models.MembershipTier.objects.filter(organization=organization).distinct()

    @route.post(
        "/membership-tiers",
        url_name="create_membership_tier",
        response={201: schema.MembershipTierSchema},
        permissions=[OrganizationPermission("manage_members")],
    )
    def create_membership_tier(
        self, slug: str, payload: schema.MembershipTierCreateSchema
    ) -> tuple[int, models.MembershipTier]:
        """Create a new membership tier for the organization."""
        organization = self.get_one(slug)
        tier = models.MembershipTier.objects.create(organization=organization, **payload.model_dump())
        return 201, tier

    @route.put(
        "/membership-tiers/{tier_id}",
        url_name="update_membership_tier",
        response=schema.MembershipTierSchema,
        permissions=[OrganizationPermission("manage_members")],
    )
    def update_membership_tier(
        self, slug: str, tier_id: UUID, payload: schema.MembershipTierUpdateSchema
    ) -> models.MembershipTier:
        """Update a membership tier."""
        organization = self.get_one(slug)
        tier = get_object_or_404(models.MembershipTier, pk=tier_id, organization=organization)
        return update_db_instance(tier, payload)

    @route.delete(
        "/membership-tiers/{tier_id}",
        url_name="delete_membership_tier",
        response={204: None},
        permissions=[OrganizationPermission("manage_members")],
    )
    def delete_membership_tier(self, slug: str, tier_id: UUID) -> tuple[int, None]:
        """Delete a membership tier.

        Members assigned to this tier will have their tier set to NULL (due to SET_NULL on the FK).
        """
        organization = self.get_one(slug)
        tier = get_object_or_404(models.MembershipTier, pk=tier_id, organization=organization)
        tier.delete()
        return 204, None

    # ---- Staff ----

    @route.get(
        "/staff",
        url_name="list_organization_staff",
        response=PaginatedResponseSchema[schema.OrganizationStaffSchema],
        permissions=[OrganizationPermission("manage_members")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["user__username", "user__email", "user__first_name", "user__last_name"])
    def list_staff(self, slug: str) -> QuerySet[models.OrganizationStaff]:
        """List all staff of an organization."""
        organization = self.get_one(slug)
        return models.OrganizationStaff.objects.filter(organization=organization).select_related("user").distinct()

    @route.delete(
        "/staff/{user_id}",
        url_name="remove_organization_staff",
        response={204: None},
        permissions=[OrganizationPermission("manage_members")],
    )
    def remove_staff(self, slug: str, user_id: UUID) -> tuple[int, None]:
        """Remove a staff member from an organization."""
        organization = self.get_one(slug)
        user_to_remove = get_object_or_404(RevelUser, id=user_id)
        organization_service.remove_staff(organization, user_to_remove)
        return 204, None

    @route.post(
        "/staff/{user_id}",
        url_name="create_organization_staff",
        response={201: schema.OrganizationStaffSchema},
        permissions=[OrganizationPermission("manage_members")],
    )
    def add_staff(
        self, slug: str, user_id: UUID, payload: models.PermissionsSchema | None = None
    ) -> tuple[int, models.OrganizationStaff]:
        """Add a staff member to an organization."""
        organization = self.get_one(slug)
        if organization.owner != self.user():
            raise HttpError(403, str(_("Only the organization owner can create staff members.")))
        users_to_add = get_object_or_404(RevelUser, id=user_id)
        return 201, organization_service.add_staff(organization, users_to_add, permissions=payload)

    @route.put(
        "/staff/{user_id}/permissions",
        url_name="update_staff_permissions",
        response=schema.OrganizationStaffSchema,
        permissions=[OrganizationPermission("edit_organization")],  # Only owners can do this
    )
    def update_staff_permissions(
        self, slug: str, user_id: UUID, payload: models.PermissionsSchema
    ) -> models.OrganizationStaff:
        """Update a staff member's permissions."""
        organization = self.get_one(slug)
        # Check if current user is the owner
        if organization.owner != self.user():
            raise HttpError(403, str(_("Only the organization owner can change staff permissions.")))

        staff_member = get_object_or_404(models.OrganizationStaff, organization=organization, user_id=user_id)
        return organization_service.update_staff_permissions(staff_member, payload)

    # ---- Tags ----

    @route.post(
        "/tags",
        url_name="add_organization_tags",
        response=list[TagSchema],
        permissions=[OrganizationPermission("edit_organization")],
    )
    def add_tags(self, slug: str, payload: schema.TagUpdateSchema) -> list[Tag]:
        """Add one or more tags to the organization."""
        organization = self.get_one(slug)
        organization.tags_manager.add(*payload.tags)
        return organization.tags_manager.all()

    @route.delete(
        "/tags",
        url_name="clear_organization_tags",
        response={204: None},
        permissions=[OrganizationPermission("edit_organization")],
    )
    def clear_tags(self, slug: str) -> tuple[int, None]:
        """Clear all tags from the organization."""
        organization = self.get_one(slug)
        organization.tags_manager.clear()
        return 204, None

    @route.post(
        "/tags/remove",
        url_name="remove_organization_tags",
        response=list[TagSchema],
        permissions=[OrganizationPermission("edit_organization")],
    )
    def remove_tags(self, slug: str, payload: schema.TagUpdateSchema) -> list[Tag]:
        """Remove one or more tags from the organization."""
        organization = self.get_one(slug)
        organization.tags_manager.remove(*payload.tags)
        return organization.tags_manager.all()
