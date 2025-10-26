import typing as t

from django.http import HttpRequest
from ninja_extra import (
    ControllerBase,
    api_controller,
    route,
)
from ninja_extra.exceptions import PermissionDenied
from ninja_extra.permissions import BasePermission
from ninja_jwt.authentication import JWTAuth

from accounts.models import RevelUser
from events import models, schema
from events.controllers.user_aware_controller import UserAwareController


class RootPermission(BasePermission):
    def __init__(self, action: str) -> None:
        """Store the action."""
        self.action = action

    def has_permission(self, request: HttpRequest, controller: ControllerBase) -> bool:
        """Must implement abstract method. This is due to an error in Ninja Extra.

        This Method will be ignored, only has_object_permission will be called.
        """
        return True


class EventSeriesPermission(RootPermission):
    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.EventSeries,
    ) -> bool:
        """Check if the user has permission to perform an action on a specific EventSeries."""
        organization = obj.organization
        if organization.owner_id == request.user.id:
            return True
        if staff_member := models.OrganizationStaff.objects.filter(
            organization=organization,
            user_id=request.user.id,
        ).first():
            return staff_member.has_permission(self.action)
        return False


class EventPermission(RootPermission):
    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.Event,
    ) -> bool:
        """Can edit event."""
        if obj.organization.owner_id == request.user.id:
            return True
        if staff_member := models.OrganizationStaff.objects.filter(
            organization=obj.organization,
            user_id=request.user.id,
        ).first():
            return staff_member.has_permission(self.action)
        return False


class OrganizationPermission(RootPermission):
    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.Organization,
    ) -> bool:
        """Can edit organization."""
        if obj.owner_id == request.user.id:
            return True
        if staff_member := models.OrganizationStaff.objects.filter(
            organization=obj,
            user_id=request.user.id,
        ).first():
            return staff_member.has_permission(self.action)
        return False


class QuestionnairePermission(RootPermission):
    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.OrganizationQuestionnaire,
    ) -> bool:
        """Can edit organization."""
        return OrganizationPermission(self.action).has_object_permission(request, controller, obj.organization)


class IsOrganizationOwner(RootPermission):
    def __init__(self) -> None:
        """Override init."""
        super().__init__(action="is_owner")

    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.Organization,
    ) -> bool:
        """Can edit organization."""
        if obj.owner_id == request.user.id:
            return True
        raise PermissionDenied("You must be the owner of this organization.")


class IsOrganizationStaff(RootPermission):
    def __init__(self) -> None:
        """Override init."""
        super().__init__(action="is_staff")

    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.Organization,
    ) -> bool:
        """Can edit organization."""
        if obj.owner_id == request.user.id:
            return True
        if models.OrganizationStaff.objects.filter(
            organization=obj,
            user_id=request.user.id,
        ).exists():
            return True
        raise PermissionDenied("You must be the owner of this organization.")


class ManagePotluckPermission(RootPermission):
    def __init__(self) -> None:
        """Init PotluckPermission."""
        super().__init__(action="manage_potluck")

    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.PotluckItem,
    ) -> bool:
        """Can edit organization."""
        if obj.created_by_id == request.user.id:
            return True
        if models.Organization.objects.filter(id=obj.event.organization_id, owner_id=request.user.id).exists():
            return True
        if staff_member := models.OrganizationStaff.objects.filter(
            organization_id=obj.event.organization_id,
            user_id=request.user.id,
        ).first():
            return staff_member.has_permission(self.action)
        return False


class PotluckItemPermission(RootPermission):
    def has_object_permission(self, request: HttpRequest, controller: ControllerBase, obj: models.Event) -> bool:
        """Can create a potluck item."""
        user = t.cast(RevelUser, request.user)
        if obj.organization.owner_id == user.id:
            return True

        if models.OrganizationStaff.objects.filter(organization=obj.organization, user=user).exists():
            return True

        if not obj.potluck_open and self.action == "create_potluck_item":
            return False

        if models.Ticket.objects.filter(event=obj, user=user).exists():
            return True

        if models.EventRSVP.objects.filter(event=obj, user=user, status=models.EventRSVP.Status.YES).exists():
            return True

        return False


class CanPurchaseTicket(RootPermission):
    def __init__(self) -> None:
        """Override init."""
        super().__init__(action="can_purchase")

    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.TicketTier,
    ) -> bool:
        """Can edit organization."""
        user = t.cast(RevelUser, request.user)
        if not obj.can_purchase():
            raise PermissionDenied("You're outside of the sale window.")
        if obj.purchasable_by == models.TicketTier.PurchasableBy.PUBLIC:
            return True
        if models.Organization.objects.filter(id=obj.event.organization_id, owner_id=user.id).exists():
            return True
        if models.OrganizationStaff.objects.filter(
            organization_id=obj.event.organization_id,
            user_id=user.id,
        ).exists():
            return True
        if obj.purchasable_by in [
            models.TicketTier.PurchasableBy.MEMBERS,
            models.TicketTier.PurchasableBy.INVITED_AND_MEMBERS,
        ]:
            if models.OrganizationMember.objects.filter(
                organization_id=obj.event.organization_id,
                user_id=user.id,
            ).exists():
                return True
        if obj.purchasable_by in [
            models.TicketTier.PurchasableBy.INVITED,
            models.TicketTier.PurchasableBy.INVITED_AND_MEMBERS,
        ]:
            if models.EventInvitation.objects.filter(
                event_id=obj.event.id,
                user_id=user.id,
            ).exists():
                return True

        raise PermissionDenied(f"The ticket can be purchased by {obj.get_purchasable_by_display()}")


@api_controller("/permissions", auth=JWTAuth(), tags=["Permissions"])
class PermissionController(UserAwareController):
    @route.get(
        "/my-permissions",
        url_name="my_permissions",
        response=schema.OrganizationPermissionsSchema,
    )
    def my_permissions(self) -> schema.OrganizationPermissionsSchema:
        """Get a user's permission map, per organization."""
        user = self.user()
        perms = models.OrganizationStaff.objects.select_related("organization").filter(user=user)
        owner_perms = {
            str(org_id): "owner"
            for org_id in models.Organization.objects.filter(owner=user).all().values_list("id", flat=True)
        }
        staff_perms = {str(perm.organization.id): perm.permissions for perm in perms}
        permissions = {**staff_perms, **owner_perms}
        memberships = list(
            models.OrganizationMember.objects.filter(user=user).values_list("organization_id", flat=True)
        )
        return schema.OrganizationPermissionsSchema(organization_permissions=permissions, memberships=memberships)
