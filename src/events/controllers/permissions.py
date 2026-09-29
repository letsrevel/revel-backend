import typing as t
from uuid import UUID

from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from ninja_extra import (
    ControllerBase,
    api_controller,
    route,
)
from ninja_extra.exceptions import PermissionDenied
from ninja_extra.permissions import BasePermission

from accounts.models import RevelUser
from common.authentication import I18nJWTAuth, OAuthPrincipal
from common.controllers import UserAwareController
from events import models, schema
from events.service import permission_snapshot


def scope_allows(request: HttpRequest, action: models.PermissionKey) -> None:
    """Raise ``InsufficientScopeError`` when an app token lacks a scope that unlocks ``action``.

    No-op for session principals. Runs *before* any owner/creator short-circuit so that an
    organization owner holding a narrowly-scoped app token cannot bypass the scope: an app
    token's effective power is its granted scopes intersected with the user's current
    organization permissions (spec §7.3).

    Args:
        request: The request being authorized; ``request.auth`` is an ``OAuthPrincipal``
            only for third-party app tokens.
        action: The ``PermissionMap`` key the calling permission class resolves.

    Raises:
        InsufficientScopeError: The principal is an app token and none of its granted
            scopes unlock ``action``.
    """
    principal = getattr(request, "auth", None)
    if not isinstance(principal, OAuthPrincipal):
        return
    from oauth.exceptions import InsufficientScopeError
    from oauth.scopes import scopes_for_key

    needed = scopes_for_key(action)
    if not needed & principal.scopes:
        # ``sorted(needed)[0]`` is deterministic only because Task 2's
        # ``test_each_key_maps_to_exactly_one_scope`` guarantees at most one scope per key.
        # An unscoped key names no scope in the challenge header — never the raw
        # ``PermissionKey``, which no client could ever request (R-27/R-45).
        raise InsufficientScopeError(sorted(needed)[0] if needed else None)


class RootPermission(BasePermission):
    def __init__(self, action: str) -> None:
        """Store the action."""
        self.action = action

    def has_permission(self, request: HttpRequest, controller: ControllerBase) -> bool:
        """Must implement abstract method. This is due to an error in Ninja Extra.

        This Method will be ignored, only has_object_permission will be called.
        """
        return True


class PermissionMapPermission(RootPermission):
    """Base for permissions whose ``action`` is resolved against ``PermissionMap``.

    Constraining ``action`` to :data:`~events.models.PermissionKey` makes mypy (and the
    IDE) reject any key that is not a real ``PermissionMap`` field — a bogus key can never
    be granted, so it would silently deny all non-owner staff (see #683).
    """

    #: Narrowed from ``RootPermission``'s ``str`` so ``scope_allows`` and the permission map
    #: take ``self.action`` as-is; the constructor below is what keeps the narrowing honest.
    action: models.PermissionKey

    def __init__(self, action: models.PermissionKey) -> None:
        """Store the map-backed action."""
        super().__init__(action=action)


class EventSeriesPermission(PermissionMapPermission):
    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.EventSeries,
    ) -> bool:
        """Check if the user has permission to perform an action on a specific EventSeries."""
        scope_allows(request, self.action)
        return obj.organization.has_org_permission(t.cast(UUID, request.user.id), self.action)


class EventPermission(PermissionMapPermission):
    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.Event,
    ) -> bool:
        """Can edit event."""
        scope_allows(request, self.action)
        return obj.organization.has_org_permission(t.cast(UUID, request.user.id), self.action)


class OrganizationPermission(PermissionMapPermission):
    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.Organization,
    ) -> bool:
        """Can edit organization."""
        scope_allows(request, self.action)
        return obj.has_org_permission(t.cast(UUID, request.user.id), self.action)


class QuestionnairePermission(PermissionMapPermission):
    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: models.OrganizationQuestionnaire,
    ) -> bool:
        """Can edit organization."""
        scope_allows(request, self.action)
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
        raise PermissionDenied(str(_("You must be the owner of this organization.")))


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
        raise PermissionDenied(str(_("You must be the owner or a staff member of this organization.")))


class CanDuplicateEvent(RootPermission):
    """Permission to duplicate an event.

    Requires create_event permission on the event's organization.
    This ensures the user can create new events in the same organization.
    """

    action: models.PermissionKey

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
        scope_allows(request, self.action)
        return obj.organization.has_org_permission(t.cast(UUID, request.user.id), self.action)


class ManagePotluckPermission(RootPermission):
    action: models.PermissionKey

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
        scope_allows(request, self.action)
        if obj.created_by_id == request.user.id:
            return True
        return obj.event.organization.has_org_permission(t.cast(UUID, request.user.id), self.action)


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
        if obj.sales_paused:
            raise PermissionDenied(str(_("Ticket sales are paused.")))
        if not obj.can_purchase():
            raise PermissionDenied(str(_("You're outside of the sale window.")))
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

        raise PermissionDenied(str(_("The ticket can be purchased by {}").format(obj.get_purchasable_by_display())))


@api_controller("/permissions", auth=I18nJWTAuth(), tags=["Permissions"])
class PermissionController(UserAwareController):
    @route.get(
        "/my-permissions",
        url_name="my_permissions",
        response=schema.OrganizationPermissionsSchema,
    )
    def my_permissions(self) -> dict[str, t.Any]:
        """Get a user's permission map, per organization.

        Served from a short-TTL per-user cache (#880) — see
        ``events.service.permission_snapshot`` for the staleness/safety model.
        The returned dict is re-validated against ``OrganizationPermissionsSchema``
        by ninja, so a corrupt cache entry fails loudly rather than silently.
        """
        return permission_snapshot.get_my_permissions_payload(self.user())
