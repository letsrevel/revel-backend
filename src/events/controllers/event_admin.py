import typing as t
from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from ninja import File, Query, Schema
from ninja.errors import HttpError
from ninja.files import UploadedFile
from ninja_extra import (
    api_controller,
    route,
)
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching
from ninja_jwt.authentication import JWTAuth

from common.models import Tag
from common.schema import TagSchema, ValidationErrorResponse
from common.throttling import UserDefaultThrottle, WriteThrottle
from common.utils import safe_save_uploaded_file
from events import filters, models, schema
from events.service import event_service, update_db_instance
from events.service.invitation_service import (
    create_direct_invitations,
    delete_invitation,
)
from events.service.ticket_notification_service import notify_ticket_status_change
from events.service.ticket_service import check_in_ticket

from ..models import EventInvitationRequest
from ..tasks import notify_event_open
from .permissions import EventPermission
from .user_aware_controller import UserAwareController


class All(Schema):
    pass


@api_controller(
    "/event-admin/{event_id}",
    auth=JWTAuth(),
    permissions=[EventPermission("invite_to_event")],
    tags=["Event Admin"],
    throttle=WriteThrottle(),
)
class EventAdminController(UserAwareController):
    def get_queryset(self) -> QuerySet[models.Event]:
        """Get the queryset based on the user."""
        return models.Event.objects.for_user(self.user(), include_past=True)

    def get_one(self, event_id: UUID) -> models.Event:
        """Wrapper helper."""
        return t.cast(models.Event, self.get_object_or_exception(self.get_queryset(), pk=event_id))

    @route.put(
        "/token/{token_id}",
        url_name="edit_event_token",
        response=schema.EventTokenSchema,
    )
    def update_event_token(
        self, event_id: UUID, token_id: str, payload: schema.EventTokenUpdateSchema
    ) -> models.EventToken:
        """Update an event token."""
        event = self.get_one(event_id)
        if payload.invitation_tier_id:
            get_object_or_404(models.TicketTier, pk=payload.invitation_tier_id, event=event)
        token = get_object_or_404(models.EventToken, pk=token_id)
        return update_db_instance(token, payload)

    @route.delete(
        "/token/{token_id}",
        url_name="delete_event_token",
        response={204: None},
    )
    def delete_event_token(self, event_id: UUID, token_id: str) -> tuple[int, None]:
        """Delete an event token."""
        event = self.get_one(event_id)
        token = get_object_or_404(models.EventToken, pk=token_id, event=event)
        token.delete()
        return 204, None

    @route.get(
        "/tokens",
        url_name="list_event_tokens",
        response=PaginatedResponseSchema[schema.EventTokenSchema],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["event__name", "event__description", "message"])
    def list_event_tokens(
        self,
        event_id: UUID,
        params: filters.EventTokenFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.EventToken]:
        """List all event tokens."""
        self.get_one(event_id)
        return params.filter(models.EventToken.objects.filter(event_id=event_id))

    @route.post(
        "/token",
        url_name="create_event_token",
        response=schema.EventTokenSchema,
    )
    def create_event_token(self, event_id: UUID, payload: schema.EventTokenCreateSchema) -> models.EventToken:
        """Create a new event token."""
        event = self.get_one(event_id)
        if payload.invitation_tier_id:
            get_object_or_404(models.TicketTier, pk=payload.invitation_tier_id, event=event)
        return event_service.create_event_token(
            event=event, issuer=self.user(), **payload.model_dump(exclude={"tier_id"})
        )

    @route.post(
        "/invitation-request/{request_id}/{decision}",
        url_name="decide_invitation_request",
        response=schema.EventInvitationRequestSchema,
    )
    def decide_invitation_request(
        self, event_id: UUID, request_id: UUID, decision: t.Literal["approve", "reject"]
    ) -> EventInvitationRequest:
        """Request an invitation to an event."""
        self.get_one(event_id)
        invitation_request = get_object_or_404(EventInvitationRequest, pk=request_id)
        if decision == "approve":
            return event_service.approve_invitation_request(invitation_request, decided_by=self.user())
        return event_service.reject_invitation_request(invitation_request, decided_by=self.user())

    @route.get(
        "/invitation_requests",
        url_name="list_event_invitation_requests",
        response=PaginatedResponseSchema[schema.EventInvitationRequestInternalSchema],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["event__name", "event__description", "message"])
    def list_event_invitation_requests(
        self,
        event_id: UUID,
        status: models.EventInvitationRequest.Status = models.EventInvitationRequest.Status.PENDING,
    ) -> QuerySet[models.EventInvitationRequest]:
        """List all pending invitation requests for the current user."""
        self.get_object_or_exception(models.Event, pk=event_id)
        return models.EventInvitationRequest.objects.select_related("user", "event").filter(
            event_id=event_id, status=status
        )

    @route.put(
        "",
        url_name="edit_event",
        response={200: schema.EventDetailSchema, 400: ValidationErrorResponse},
        permissions=[EventPermission("edit_event")],
    )
    def update_event(self, event_id: UUID, payload: schema.EventEditSchema) -> models.Event:
        """Update event by ID."""
        event = self.get_one(event_id)
        return update_db_instance(event, payload)

    @route.post(
        "/actions/update-status/{status}",
        url_name="update_event_status",
        permissions=[EventPermission("manage_event")],
        response=schema.EventDetailSchema,
    )
    def update_event_status(self, event_id: UUID, status: models.Event.Status) -> models.Event:
        """Update event status to the specified value."""
        event = self.get_one(event_id)
        old_status = event.status
        event.status = status
        event.save(update_fields=["status"])

        # Send notification if event is being opened
        if old_status != models.Event.Status.OPEN and status == models.Event.Status.OPEN:
            notify_event_open.delay(str(event.id))

        return event

    @route.post(
        "/upload-logo",
        url_name="event_upload_logo",
        response=schema.EventDetailSchema,
        permissions=[EventPermission("edit_event")],
    )
    def upload_logo(self, event_id: UUID, logo: File[UploadedFile]) -> models.Event:
        """Upload logo to event."""
        event = self.get_one(event_id)
        event = safe_save_uploaded_file(instance=event, field="logo", file=logo, uploader=self.user())
        return event

    @route.post(
        "/upload-cover-art",
        url_name="event_upload_cover_art",
        response=schema.EventDetailSchema,
        permissions=[EventPermission("edit_event")],
    )
    def upload_cover_art(self, event_id: UUID, cover_art: File[UploadedFile]) -> models.Event:
        """Upload cover art to event."""
        event = self.get_one(event_id)
        event = safe_save_uploaded_file(instance=event, field="cover_art", file=cover_art, uploader=self.user())
        return event

    @route.post(
        "/tags",
        url_name="add_event_tags",
        response=list[TagSchema],
        permissions=[EventPermission("edit_event")],
    )
    def add_tags(self, event_id: UUID, payload: schema.TagUpdateSchema) -> list[Tag]:
        """Add one or more tags to the organization."""
        event = self.get_one(event_id)
        event.tags_manager.add(*payload.tags)
        return event.tags_manager.all()

    @route.delete(
        "/tags",
        url_name="clear_event_tags",
        response={204: None},
        permissions=[EventPermission("edit_event")],
    )
    def clear_tags(self, event_id: UUID) -> tuple[int, None]:
        """Remove one or more tags from the organization."""
        event = self.get_one(event_id)
        event.tags_manager.clear()
        return 204, None

    @route.post(
        "/tags/remove",
        url_name="remove_event_tags",
        response=list[TagSchema],
        permissions=[EventPermission("edit_event")],
    )
    def remove_tags(self, event_id: UUID, payload: schema.TagUpdateSchema) -> list[Tag]:
        """Remove one or more tags from the organization."""
        event = self.get_one(event_id)
        event.tags_manager.remove(*payload.tags)
        return event.tags_manager.all()

    @route.get(
        "/ticket-tiers",
        url_name="list_ticket_tiers",
        response=PaginatedResponseSchema[schema.TicketTierDetailSchema],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_ticket_tiers(self, event_id: UUID) -> QuerySet[models.TicketTier]:
        """List all ticket tiers for an event."""
        self.get_one(event_id)
        return models.TicketTier.objects.filter(event_id=event_id).order_by("price", "name")

    @route.post(
        "/ticket-tier",
        url_name="create_ticket_tier",
        response=schema.TicketTierDetailSchema,
        permissions=[EventPermission("manage_tickets")],
    )
    def create_ticket_tier(self, event_id: UUID, payload: schema.TicketTierCreateSchema) -> models.TicketTier:
        """Create a new ticket tier for an event."""
        event = self.get_one(event_id)
        if (
            payload.payment_method == models.TicketTier.PaymentMethod.ONLINE
            and not event.organization.is_stripe_connected
        ):
            raise HttpError(400, "You must connect to Stripe first.")
        return models.TicketTier.objects.create(event=event, **payload.model_dump())

    @route.put(
        "/ticket-tier/{tier_id}",
        url_name="update_ticket_tier",
        response=schema.TicketTierDetailSchema,
        permissions=[EventPermission("manage_tickets")],
    )
    def update_ticket_tier(
        self, event_id: UUID, tier_id: UUID, payload: schema.TicketTierUpdateSchema
    ) -> models.TicketTier:
        """Update a ticket tier."""
        event = self.get_one(event_id)
        if (
            payload.payment_method == models.TicketTier.PaymentMethod.ONLINE
            and not event.organization.is_stripe_connected
        ):
            raise HttpError(400, "You must connect to Stripe first.")
        tier = get_object_or_404(models.TicketTier, pk=tier_id, event=event)
        return update_db_instance(tier, payload)

    @route.delete(
        "/ticket-tier/{tier_id}",
        url_name="delete_ticket_tier",
        response={204: None},
        permissions=[EventPermission("manage_tickets")],
    )
    def delete_ticket_tier(self, event_id: UUID, tier_id: UUID) -> tuple[int, None]:
        """Delete a ticket tier.

        Note this might raise a 400 if ticket with this tier where already bought.
        """
        event = self.get_one(event_id)
        tier = get_object_or_404(models.TicketTier, pk=tier_id, event=event)
        tier.delete()
        return 204, None

    @route.get(
        "/pending-tickets",
        url_name="list_pending_tickets",
        response=PaginatedResponseSchema[schema.PendingTicketSchema],
        permissions=[EventPermission("manage_tickets")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["user__email", "user__first_name", "user__last_name", "tier__name"])
    def list_pending_tickets(self, event_id: UUID) -> QuerySet[models.Ticket]:
        """List all pending tickets for offline and at-the-door payment methods."""
        event = self.get_one(event_id)
        return models.Ticket.objects.select_related("user", "tier").filter(
            event=event,
            status=models.Ticket.Status.PENDING,
            tier__payment_method__in=[
                models.TicketTier.PaymentMethod.OFFLINE,
                models.TicketTier.PaymentMethod.AT_THE_DOOR,
            ],
        )

    @route.post(
        "/tickets/{ticket_id}/confirm-payment",
        url_name="confirm_ticket_payment",
        response={200: schema.EventTicketSchema},
        permissions=[EventPermission("manage_tickets")],
    )
    def confirm_ticket_payment(self, event_id: UUID, ticket_id: UUID) -> models.Ticket:
        """Confirm payment for a pending offline ticket and activate it."""
        event = self.get_one(event_id)
        ticket = get_object_or_404(
            models.Ticket,
            pk=ticket_id,
            event=event,
            status=models.Ticket.Status.PENDING,
            tier__payment_method__in=[
                models.TicketTier.PaymentMethod.OFFLINE,
                models.TicketTier.PaymentMethod.AT_THE_DOOR,
            ],
        )
        old_status = ticket.status
        ticket.status = models.Ticket.Status.ACTIVE
        ticket.save(update_fields=["status"])

        # Send ticket activation notification
        notify_ticket_status_change(str(ticket.id), old_status)

        return ticket

    @route.post(
        "/check-in",
        url_name="check_in_ticket",
        response={200: schema.CheckInResponseSchema, 400: ValidationErrorResponse},
        permissions=[EventPermission("check_in_attendees")],
    )
    def check_in_ticket(self, event_id: UUID, payload: schema.CheckInRequestSchema) -> models.Ticket:
        """Check in an attendee by scanning their ticket."""
        event = self.get_one(event_id)
        return check_in_ticket(event, payload.ticket_id, self.user())

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
        return models.EventInvitation.objects.filter(event=event)

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
        return models.PendingEventInvitation.objects.filter(event=event)

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
        raise HttpError(404, "Invitation not found.")
