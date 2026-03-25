import typing as t

from django.http import HttpRequest
from ninja_extra import (
    ControllerBase,
    api_controller,
    route,
)
from ninja_extra.exceptions import PermissionDenied
from ninja_extra.permissions import BasePermission

from accounts.models import RevelUser
from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from events import models, schema
from events.models import OrganizationMember


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
        return obj.organization.has_org_permission(request.user.id, self.action)


class EventPermission(RootPermission):
    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.Event,
    ) -> bool:
        """Can edit event."""
        return obj.organization.has_org_permission(request.user.id, self.action)


class OrganizationPermission(RootPermission):
    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.Organization,
    ) -> bool:
        """Can edit organization."""
        return obj.has_org_permission(request.user.id, self.action)


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
        if obj.is_owner_or_staff(t.cast(RevelUser, request.user)):
            return True
        raise PermissionDenied("You must be the owner of this organization.")


class CanDuplicateEvent(RootPermission):
    """Permission to duplicate an event.

    Requires create_event permission on the event's organization.
    This ensures the user can create new events in the same organization.
    """

    def __init__(self) -> None:
        """Initialize with create_event action."""
        super().__init__(action="create_event")

    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.Event,
    ) -> bool:
        """Check if user can duplicate this event (create new event in same org)."""
        return obj.organization.has_org_permission(request.user.id, self.action)


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
        return obj.event.organization.has_org_permission(request.user.id, self.action)


class PotluckItemPermission(RootPermission):
    def has_object_permission(self, request: HttpRequest, controller: ControllerBase, obj: models.Event) -> bool:
        """Can create a potluck item."""
        user = t.cast(RevelUser, request.user)
        if obj.organization.is_owner_or_staff(user):
            return True

        if not obj.potluck_open and self.action == "create_potluck_item":
            return False

        if models.Ticket.objects.filter(event=obj, user=user).exists():
            return True

        if models.EventRSVP.objects.filter(event=obj, user=user, status=models.EventRSVP.RsvpStatus.YES).exists():
            return True

        return False


class CanPurchaseTicket(RootPermission):
    def __init__(self) -> None:
        """Override init."""
        super().__init__(action="can_purchase")

    def _check_invited(self, tier: models.TicketTier, user_id: t.Any) -> bool:
        """Check if user has a valid invitation for this tier."""
        invitation = models.EventInvitation.objects.filter(event_id=tier.event_id, user_id=user_id).first()
        if not invitation:
            return False
        if tier.restrict_purchase_to_linked_invitations:
            return invitation.tiers.filter(pk=tier.pk).exists()
        return True

    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.TicketTier,
    ) -> bool:
        """Check if user can purchase from this tier."""
        user = t.cast(RevelUser, request.user)
        if not obj.can_purchase():
            raise PermissionDenied("You're outside of the sale window.")
        if obj.purchasable_by == models.TicketTier.PurchasableBy.PUBLIC:
            return True
        if obj.event.organization.is_owner_or_staff(user):
            return True

        PB = models.TicketTier.PurchasableBy
        is_member = (
            models.OrganizationMember.objects.active_only()
            .filter(organization_id=obj.event.organization_id, user_id=user.id)
            .exists()
        )

        if obj.purchasable_by in [PB.MEMBERS, PB.INVITED_AND_MEMBERS] and is_member:
            return True
        if obj.purchasable_by in [PB.INVITED, PB.INVITED_AND_MEMBERS] and self._check_invited(obj, user.id):
            return True

        raise PermissionDenied(f"The ticket can be purchased by {obj.get_purchasable_by_display()}")


@api_controller("/permissions", auth=I18nJWTAuth(), tags=["Permissions"])
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

        # Build memberships dict with minimal member info
        memberships: dict[str, schema.MinimalOrganizationMemberSchema] = {}
        members = (
            models.OrganizationMember.objects.select_related("tier")
            .filter(user=user)
            .exclude(status=OrganizationMember.MembershipStatus.BANNED)
        )
        for member in members:
            org_id_str = str(member.organization_id)
            memberships[org_id_str] = schema.MinimalOrganizationMemberSchema.from_orm(member)

        return schema.OrganizationPermissionsSchema(organization_permissions=permissions, memberships=memberships)
