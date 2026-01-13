from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja import Query
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import filters, models, schema
from events.controllers.permissions import EventPermission
from events.models import EventInvitationRequest
from events.service import event_service

from .base import EventAdminBaseController


@api_controller(
    "/event-admin/{event_id}",
    auth=I18nJWTAuth(),
    permissions=[EventPermission("invite_to_event")],
    tags=["Event Admin"],
    throttle=WriteThrottle(),
)
class EventAdminInvitationRequestsController(EventAdminBaseController):
    """Event invitation request management endpoints."""

    @route.get(
        "/invitation-requests",
        url_name="list_invitation_requests",
        response=PaginatedResponseSchema[schema.EventInvitationRequestInternalSchema],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["event__name", "event__description", "message"])
    def list_invitation_requests(
        self,
        event_id: UUID,
        params: filters.InvitationRequestFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.EventInvitationRequest]:
        """List all invitation requests for an event.

        By default shows all requests. Use ?status=pending to filter by status.
        """
        self.get_one(event_id)
        qs = models.EventInvitationRequest.objects.select_related("user", "event").filter(event_id=event_id)
        return params.filter(qs).distinct()

    @route.post(
        "/invitation-requests/{request_id}/approve",
        url_name="approve_invitation_request",
        response={204: None},
    )
    def approve_invitation_request(self, event_id: UUID, request_id: UUID) -> tuple[int, None]:
        """Approve an invitation request."""
        event = self.get_one(event_id)
        invitation_request = get_object_or_404(EventInvitationRequest, pk=request_id, event=event)
        event_service.approve_invitation_request(invitation_request, decided_by=self.user())
        return 204, None

    @route.post(
        "/invitation-requests/{request_id}/reject",
        url_name="reject_invitation_request",
        response={204: None},
    )
    def reject_invitation_request(self, event_id: UUID, request_id: UUID) -> tuple[int, None]:
        """Reject an invitation request."""
        event = self.get_one(event_id)
        invitation_request = get_object_or_404(EventInvitationRequest, pk=request_id, event=event)
        event_service.reject_invitation_request(invitation_request, decided_by=self.user())
        return 204, None
