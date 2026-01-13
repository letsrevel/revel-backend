import typing as t
from uuid import UUID

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.authentication import I18nJWTAuth
from common.schema import ValidationErrorResponse
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import models, schema
from events.controllers.permissions import EventPermission
from events.service.invitation_service import create_direct_invitations, delete_invitation

from .base import EventAdminBaseController


@api_controller(
    "/event-admin/{event_id}",
    auth=I18nJWTAuth(),
    permissions=[EventPermission("invite_to_event")],
    tags=["Event Admin"],
    throttle=WriteThrottle(),
)
class EventAdminInvitationsController(EventAdminBaseController):
    """Event invitation management endpoints."""

    @route.post(
        "/invitations",
        url_name="create_direct_invitations",
        response={200: schema.DirectInvitationResponseSchema, 400: ValidationErrorResponse},
        permissions=[EventPermission("invite_to_event")],
    )
    def create_invitations(self, event_id: UUID, payload: schema.DirectInvitationCreateSchema) -> dict[str, int]:
        """Create direct invitations for users by email addresses."""
        event = self.get_one(event_id)
        return create_direct_invitations(event, payload)

    @route.get(
        "/invitations",
        url_name="list_event_invitations",
        response=PaginatedResponseSchema[schema.EventInvitationListSchema],
        permissions=[EventPermission("invite_to_event")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["user__email", "user__first_name", "user__last_name", "custom_message"])
    def list_invitations(self, event_id: UUID) -> QuerySet[models.EventInvitation]:
        """List all invitations for registered users."""
        event = self.get_one(event_id)
        return models.EventInvitation.objects.with_related().filter(event=event).distinct()

    @route.get(
        "/pending-invitations",
        url_name="list_pending_invitations",
        response=PaginatedResponseSchema[schema.PendingEventInvitationListSchema],
        permissions=[EventPermission("invite_to_event")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["email", "custom_message"])
    def list_pending_invitations(
        self,
        event_id: UUID,
    ) -> QuerySet[models.PendingEventInvitation]:
        """List all pending invitations for unregistered users."""
        event = self.get_one(event_id)
        return models.PendingEventInvitation.objects.filter(event=event).distinct()

    @route.delete(
        "/invitations/{invitation_type}/{invitation_id}",
        url_name="delete_invitation",
        response={204: None, 404: ValidationErrorResponse},
        permissions=[EventPermission("invite_to_event")],
    )
    def delete_invitation_endpoint(
        self, event_id: UUID, invitation_type: t.Literal["registered", "pending"], invitation_id: UUID
    ) -> tuple[int, None]:
        """Delete an invitation (registered or pending)."""
        event = self.get_one(event_id)

        if delete_invitation(event, invitation_id, invitation_type):
            return 204, None
        raise HttpError(404, str(_("Invitation not found.")))
