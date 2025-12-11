import typing as t
from uuid import UUID

from django.db import transaction
from django.db.models import F, QuerySet
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja import File, Query, Schema
from ninja.errors import HttpError
from ninja.files import UploadedFile
from ninja_extra import (
    api_controller,
    route,
)
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from accounts.models import RevelUser
from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.models import Tag
from common.schema import TagSchema, ValidationErrorResponse
from common.throttling import UserDefaultThrottle, WriteThrottle
from common.utils import safe_save_uploaded_file
from events import filters, models, schema
from events.service import event_service, ticket_service, update_db_instance
from events.service.invitation_service import (
    create_direct_invitations,
    delete_invitation,
)
from events.service.ticket_service import check_in_ticket

from ..models import EventInvitationRequest
from .permissions import CanDuplicateEvent, EventPermission


class All(Schema):
    pass


@api_controller(
    "/event-admin/{event_id}",
    auth=I18nJWTAuth(),
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
        "/tokens/{token_id}",
        url_name="edit_event_token",
        response=schema.EventTokenSchema,
    )
    def update_event_token(
        self, event_id: UUID, token_id: str, payload: schema.EventTokenUpdateSchema
    ) -> models.EventToken:
        """Update an existing event token's configuration.

        Event tokens are shareable codes/links that allow users to claim invitations to your event.
        Use this endpoint to modify token settings after creation.

        **Use Cases:**
        - Change the expiration date to extend or shorten token validity
        - Update the maximum number of uses (e.g., limit invites to 50 people)
        - Change which ticket tier users get when claiming (e.g., switch from VIP to General)
        - Update the token name for better organization
        - Modify custom invitation metadata (welcome message, special instructions, etc.)

        **Parameters:**
        - `name`: Optional display name to help you identify this token (e.g., "Alumni Link", "Early Bird")
        - `max_uses`: Maximum number of times this token can be claimed (0 = unlimited)
        - `expires_at`: When the token becomes invalid (users can't claim after this time)
        - `ticket_tier_id`: Which ticket tier to assign when users claim (required for ticketed events)
        - `invitation`: Custom invitation metadata like welcome messages, special flags, etc.

        **Business Logic:**
        - The token's usage count (how many times it's been claimed) is NOT reset when updating
        - If you set max_uses lower than current uses, the token becomes inactive
        - Changing the tier only affects future claims, not existing invitations
        - The token ID itself never changes

        **Frontend Implementation:**
        Display a token management UI where organizers can:
        1. View token stats (uses, expiration, link)
        2. Edit token settings with a form
        3. Show validation errors if tier_id doesn't match event
        4. Warn when reducing max_uses below current usage
        """
        event = self.get_one(event_id)
        if payload.ticket_tier_id:
            get_object_or_404(models.TicketTier, pk=payload.ticket_tier_id, event=event)
        token = get_object_or_404(models.EventToken, pk=token_id)
        return update_db_instance(token, payload)

    @route.delete(
        "/tokens/{token_id}",
        url_name="delete_event_token",
        response={204: None},
    )
    def delete_event_token(self, event_id: UUID, token_id: str) -> tuple[int, None]:
        """Permanently delete an event token and invalidate all links using it.

        **Use Cases:**
        - Revoke access when a shareable link is compromised or leaked publicly
        - Clean up expired or unused tokens
        - Remove tokens after an event closes or capacity is reached
        - Invalidate outdated promotional links

        **Important Warnings:**
        - This action is IRREVERSIBLE - the token and its link become permanently invalid
        - Users with the link will no longer be able to claim invitations
        - However, users who ALREADY claimed invitations keep their access (their EventInvitations persist)
        - The token's usage statistics are lost

        **Frontend Implementation:**
        1. Show a confirmation dialog: "Delete this token? The link will stop working immediately."
        2. Clarify that existing invitations from this token remain valid
        3. Remove the token from the list immediately after successful deletion
        4. Consider showing a "copy shareable link" button before deletion for archival
        """
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
        """Retrieve all invitation tokens for this event with usage statistics.

        Event tokens serve two purposes:
        1. **Visibility** - Grant temporary access to view private events via `?et=` URL parameter
        2. **Invitations** - Allow users to claim invitations with optional ticket tier assignment

        Each token can have usage limits, expiration dates, and associated ticket tiers.

        **Returns:**
        Paginated list of tokens with:
        - `id`: The unique token code (used in shareable links and as `?et=` param)
        - `name`: Display name for organization
        - `issuer`: The user who created this token
        - `expires_at`: When the token stops working (null = never expires)
        - `uses`: How many times it's been claimed so far
        - `max_uses`: Maximum allowed claims (0 = unlimited)
        - `ticket_tier`: Which ticket tier users get when claiming
        - `invitation_payload`: Custom metadata (welcome message, flags, etc.)
        - `created_at`: When the token was created

        **Filtering & Search:**
        - Search by token name, event name/description, or custom message
        - Filter by expiration status, tier, or usage count (via params)
        - Results are paginated (20 per page by default)

        **Frontend Implementation:**
        Build a token management dashboard showing:
        1. **Token List Table:**
           - Token name (with copy-link button)
           - Usage: "23 / 50 uses" or "15 uses (unlimited)"
           - Status badge: "Active" (green), "Expired" (red), "Limit Reached" (yellow)
           - Tier name if specified
           - Expiration date
           - Actions: Edit, Delete, Copy Link

        2. **Shareable Link Format:**
           - For visibility: `https://yourapp.com/events/{event_id}?et={token_id}`
             (Frontend extracts `?et=` and sends as `X-Event-Token` header to API)
           - For claiming: `https://yourapp.com/invite/event/{token_id}` → POST `/events/claim-invitation/{token_id}`

        3. **Status Indicators:**
           ```javascript
           function getTokenStatus(token) {
             if (token.expires_at && new Date(token.expires_at) < new Date()) return "Expired";
             if (token.max_uses > 0 && token.uses >= token.max_uses) return "Limit Reached";
             return "Active";
           }
           ```

        4. **Analytics Display:**
           - Show usage percentage as progress bar
           - Display "# claimed today" if tracking recent activity
           - Show which tier most users are getting

        **Use Cases:**
        - Display all invitation links in the event admin panel
        - Monitor token usage for capacity planning
        - Identify which promotional channels are most effective
        - Audit who created which tokens and when
        """
        self.get_one(event_id)
        return params.filter(models.EventToken.objects.filter(event_id=event_id)).distinct()

    @route.post(
        "/tokens",
        url_name="create_event_token",
        response=schema.EventTokenSchema,
    )
    def create_event_token(self, event_id: UUID, payload: schema.EventTokenCreateSchema) -> models.EventToken:
        """Create a new shareable token for this event.

        Event tokens serve dual purposes:
        1. **Primary: Visibility** - Share links like `/events/{id}?et={token}` to let non-members view private events
        2. **Secondary: Invitations** - Optionally allow users to claim event invitations with ticket tier assignment

        This enables sharing event details in group chats, social media, or with non-members without
        requiring them to join first.

        **Use Cases:**
        - **Social Media Promotions:** Share on Twitter/Instagram to let followers RSVP
        - **Email Campaigns:** Include in newsletters for easy one-click registration
        - **Partner Organizations:** Give to affiliated groups for member distribution
        - **Tiered Access:** Create different tokens for VIP, General, Student tiers
        - **Time-Limited Offers:** Early bird tokens that expire before price increase
        - **Capacity Management:** Tokens with usage limits (e.g., "50 from marketing list")
        - **Referral Tracking:** Create per-channel tokens to measure effectiveness

        **Parameters:**
        - `name`: Display name for organization (e.g., "Instagram Followers", "Alumni Network")
        - `duration`: Minutes until expiration (default: 24*60 = 1 day)
        - `max_uses`: Maximum claims allowed (default: 1, use 0 for unlimited)
        - `ticket_tier_id`: Ticket tier to auto-assign (required for ticketed events, optional otherwise)
        - `invitation`: Optional custom metadata:
          - `custom_message`: Personalized welcome text
          - Additional fields that your EventInvitation model supports

        **Returns:**
        The created token with a unique `id` that serves as the shareable code.

        **Business Logic:**
        - Token issuer is automatically set to the current authenticated user
        - Expiration is calculated from current time + duration
        - If ticket_tier_id provided, validates it belongs to this event
        - For ticketed events, ticket_tier_id is REQUIRED
        - Token ID is a secure random 8-character alphanumeric code
        - Created tokens start with 0 uses

        **Frontend Implementation:**
        After creation, immediately show the shareable link:

        ```javascript
        // On successful creation:
        const shareableUrl = `https://yourapp.com/invite/event/${response.id}`;

        // Show UI with:
        - Shareable link with copy button
        - QR code for in-person distribution
        - Social media share buttons
        - Usage tracking: "0 / {max_uses} used"
        - Expiration countdown: "Expires in 23 hours"
        ```

        **Example Workflow:**
        1. Organizer creates token with name="Instagram Post", max_uses=100, duration=7*24*60
        2. Frontend displays: `https://yourapp.com/invite/event/aBc12XyZ`
        3. Organizer shares link on Instagram
        4. Users visit link → Frontend calls POST `/events/claim-invitation` → Users get EventInvitation
        5. After 100 claims or 7 days, token becomes inactive

        **Error Cases:**
        - 400: ticket_tier_id missing for ticketed event, or tier doesn't belong to this event
        - 403: User lacks "invite_to_event" permission
        - 404: event_id not found or user lacks access
        """
        event = self.get_one(event_id)
        # Validate ticket_tier_id is required for ticketed events
        if event.requires_ticket and not payload.ticket_tier_id:
            raise HttpError(400, str(_("ticket_tier_id is required for events that require tickets.")))
        if payload.ticket_tier_id:
            get_object_or_404(models.TicketTier, pk=payload.ticket_tier_id, event=event)
        return event_service.create_event_token(event=event, issuer=self.user(), **payload.model_dump())

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

    @route.delete(
        "",
        url_name="delete_event",
        response={204: None},
        permissions=[EventPermission("delete_event")],
    )
    def delete_event(self, event_id: UUID) -> tuple[int, None]:
        """Delete event by ID."""
        event = self.get_one(event_id)
        event.delete()
        return 204, None

    @route.patch(
        "/slug",
        url_name="edit_event_slug",
        response={200: schema.EventDetailSchema},
        permissions=[EventPermission("edit_event")],
    )
    def edit_slug(self, event_id: UUID, payload: schema.EventEditSlugSchema) -> models.Event:
        """Update the event's slug (URL-friendly identifier).

        The slug must be unique within the organization and must be a valid slug format
        (lowercase letters, numbers, and hyphens only).
        """
        event = self.get_one(event_id)

        # Check if slug already exists for this organization
        if (
            models.Event.objects.filter(organization_id=event.organization_id, slug=payload.slug)
            .exclude(pk=event.pk)
            .exists()
        ):
            raise HttpError(400, str(_("An event with this slug already exists in your organization.")))

        event.slug = payload.slug
        event.save(update_fields=["slug"])
        return event

    @route.post(
        "/duplicate",
        url_name="duplicate_event",
        response={200: schema.EventDetailSchema},
        permissions=[CanDuplicateEvent()],
    )
    def duplicate_event(self, event_id: UUID, payload: schema.EventDuplicateSchema) -> models.Event:
        """Create a copy of this event with a new name and start date.

        All date fields are shifted relative to the new start date. The new event
        is created in DRAFT status. Ticket tiers, suggested potluck items, tags,
        questionnaire links, and resource links are copied. User-specific data
        (tickets, RSVPs, invitations, etc.) is NOT copied.

        Requires create_event permission on the event's organization.
        """
        event = self.get_one(event_id)
        return event_service.duplicate_event(
            template_event=event,
            new_name=payload.name,
            new_start=payload.start,
        )

    @route.post(
        "/actions/update-status/{status}",
        url_name="update_event_status",
        permissions=[EventPermission("manage_event")],
        response=schema.EventDetailSchema,
    )
    def update_event_status(self, event_id: UUID, status: models.Event.EventStatus) -> models.Event:
        """Update event status to the specified value.

        Note: Event opening notifications are handled automatically by the post_save signal
        in events/signals.py which triggers when status field is updated.
        """
        event = self.get_one(event_id)
        event.status = status
        event.save(update_fields=["status"])

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

    @route.delete(
        "/delete-logo",
        url_name="event_delete_logo",
        response={204: None},
        permissions=[EventPermission("edit_event")],
    )
    def delete_logo(self, event_id: UUID) -> tuple[int, None]:
        """Delete logo from event."""
        event = self.get_one(event_id)
        if event.logo:
            event.logo.delete(save=True)
        return 204, None

    @route.delete(
        "/delete-cover-art",
        url_name="event_delete_cover_art",
        response={204: None},
        permissions=[EventPermission("edit_event")],
    )
    def delete_cover_art(self, event_id: UUID) -> tuple[int, None]:
        """Delete cover art from event."""
        event = self.get_one(event_id)
        if event.cover_art:
            event.cover_art.delete(save=True)
        return 204, None

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
        return (
            models.TicketTier.objects.with_venue_and_sector()
            .filter(event_id=event_id)
            .distinct()
            .order_by("price", "name")
        )

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
            raise HttpError(400, str(_("You must connect to Stripe first.")))

        # Extract restricted_to_membership_tiers_ids from payload
        payload_dict = payload.model_dump(exclude_unset=True)
        restricted_to_membership_tiers_ids = payload_dict.pop("restricted_to_membership_tiers_ids", None)

        # Create ticket tier with M2M handling in service layer
        tier = ticket_service.create_ticket_tier(
            event=event, tier_data=payload_dict, restricted_to_membership_tiers_ids=restricted_to_membership_tiers_ids
        )
        # Refetch with venue/sector for response serialization
        return models.TicketTier.objects.with_venue_and_sector().get(pk=tier.pk)

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
            raise HttpError(400, str(_("You must connect to Stripe first.")))

        tier = get_object_or_404(models.TicketTier, pk=tier_id, event=event)

        # Extract restricted_to_membership_tiers_ids from payload
        payload_dict = payload.model_dump(exclude_unset=True)
        restricted_to_membership_tiers_ids = payload_dict.pop("restricted_to_membership_tiers_ids", None)

        # Update ticket tier with M2M handling in service layer
        updated_tier = ticket_service.update_ticket_tier(
            tier=tier, tier_data=payload_dict, restricted_to_membership_tiers_ids=restricted_to_membership_tiers_ids
        )
        # Refetch with venue/sector for response serialization
        return models.TicketTier.objects.with_venue_and_sector().get(pk=updated_tier.pk)

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
        "/tickets",
        url_name="list_tickets",
        response=PaginatedResponseSchema[schema.AdminTicketSchema],
        permissions=[EventPermission("manage_tickets")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(
        Searching,
        search_fields=["user__email", "user__first_name", "user__last_name", "tier__name", "user__preferred_name"],
    )
    def list_tickets(
        self,
        event_id: UUID,
        params: filters.TicketFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.Ticket]:
        """List tickets for an event with optional filters.

        Supports filtering by:
        - status: Filter by ticket status (PENDING, ACTIVE, CANCELLED, CHECKED_IN)
        - tier__payment_method: Filter by payment method (ONLINE, OFFLINE, AT_THE_DOOR, FREE)
        """
        event = self.get_one(event_id)
        # Include tier with venue/sector/city for AdminTicketSchema, plus seat and payment
        qs = models.Ticket.objects.select_related(
            "user",
            "tier",
            "tier__venue",
            "tier__venue__city",
            "tier__sector",
            "seat",
            "payment",
        ).filter(event=event)
        return params.filter(qs).distinct()

    @route.get(
        "/tickets/{ticket_id}",
        url_name="get_ticket",
        response={200: schema.AdminTicketSchema},
        permissions=[EventPermission("manage_tickets")],
        throttle=UserDefaultThrottle(),
    )
    def get_ticket(self, event_id: UUID, ticket_id: UUID) -> models.Ticket:
        """Get a ticket by its ID."""
        event = self.get_one(event_id)
        return get_object_or_404(
            models.Ticket.objects.select_related(
                "user",
                "tier",
                "tier__venue",
                "tier__venue__city",
                "tier__sector",
                "seat",
                "payment",
            ),
            pk=ticket_id,
            event=event,
        )

    @route.post(
        "/tickets/{ticket_id}/confirm-payment",
        url_name="confirm_ticket_payment",
        response={200: schema.UserTicketSchema},
        permissions=[EventPermission("manage_tickets")],
    )
    def confirm_ticket_payment(self, event_id: UUID, ticket_id: UUID) -> models.Ticket:
        """Confirm payment for a pending offline ticket and activate it."""
        event = self.get_one(event_id)
        ticket = get_object_or_404(
            models.Ticket,
            pk=ticket_id,
            event=event,
            tier__payment_method__in=[
                models.TicketTier.PaymentMethod.OFFLINE,
                models.TicketTier.PaymentMethod.AT_THE_DOOR,
            ],
        )
        # Store old status before updating (signal handler needs this)
        ticket._original_ticket_status = ticket.status  # type: ignore[attr-defined]
        ticket.status = models.Ticket.TicketStatus.ACTIVE
        ticket.save(update_fields=["status"])

        # Notification sent automatically via signal handler
        # Re-fetch with full() to include all related objects for UserTicketSchema
        return models.Ticket.objects.full().get(pk=ticket.pk)

    @route.post(
        "/tickets/{ticket_id}/mark-refunded",
        url_name="mark_ticket_refunded",
        response={200: schema.UserTicketSchema},
        permissions=[EventPermission("manage_tickets")],
    )
    def mark_ticket_refunded(self, event_id: UUID, ticket_id: UUID) -> models.Ticket:
        """Mark a manual payment ticket as refunded and cancel it.

        This endpoint is for offline/at-the-door tickets only.
        Online tickets (Stripe) are automatically managed via webhooks.
        """
        event = self.get_one(event_id)
        ticket = get_object_or_404(
            models.Ticket.objects.select_related("tier", "payment"),
            pk=ticket_id,
            event=event,
            tier__payment_method__in=[
                models.TicketTier.PaymentMethod.OFFLINE,
                models.TicketTier.PaymentMethod.AT_THE_DOOR,
            ],
        )

        # Restore ticket quantity and cancel the ticket
        with transaction.atomic():
            models.TicketTier.objects.select_for_update().filter(pk=ticket.tier.pk, quantity_sold__gt=0).update(
                quantity_sold=F("quantity_sold") - 1
            )
            ticket.status = models.Ticket.TicketStatus.CANCELLED
            ticket.save(update_fields=["status"])

            # Mark the associated payment as refunded if it exists
            if hasattr(ticket, "payment"):
                ticket.payment.status = models.Payment.PaymentStatus.REFUNDED
                ticket.payment.save(update_fields=["status"])

        # Refund notification sent automatically by stripe webhook handler
        # Re-fetch with full() to include all related objects for UserTicketSchema
        return models.Ticket.objects.full().get(pk=ticket.pk)

    @route.post(
        "/tickets/{ticket_id}/cancel",
        url_name="cancel_ticket",
        response={200: schema.UserTicketSchema},
        permissions=[EventPermission("manage_tickets")],
    )
    def cancel_ticket(self, event_id: UUID, ticket_id: UUID) -> models.Ticket:
        """Cancel a manual payment ticket.

        This endpoint is for offline/at-the-door tickets only.
        Online tickets (Stripe) should be refunded via Stripe Dashboard.
        """
        event = self.get_one(event_id)
        ticket = get_object_or_404(
            models.Ticket.objects.select_related("tier"),
            pk=ticket_id,
            event=event,
            tier__payment_method__in=[
                models.TicketTier.PaymentMethod.OFFLINE,
                models.TicketTier.PaymentMethod.AT_THE_DOOR,
            ],
        )

        if ticket.status == models.Ticket.TicketStatus.CANCELLED:
            raise HttpError(400, str(_("Ticket already cancelled")))

        # Restore ticket quantity and cancel the ticket
        with transaction.atomic():
            models.TicketTier.objects.select_for_update().filter(pk=ticket.tier.pk, quantity_sold__gt=0).update(
                quantity_sold=F("quantity_sold") - 1
            )
            # Store old status before updating (signal handler needs this)
            ticket._original_ticket_status = ticket.status  # type: ignore[attr-defined]
            ticket.status = models.Ticket.TicketStatus.CANCELLED
            ticket.save(update_fields=["status"])

        # Notification sent automatically via signal handler
        # Re-fetch with full() to include all related objects for UserTicketSchema
        return models.Ticket.objects.full().get(pk=ticket.pk)

    @route.post(
        "/tickets/{ticket_id}/check-in",
        url_name="check_in_ticket",
        response={200: schema.CheckInResponseSchema, 400: ValidationErrorResponse},
        permissions=[EventPermission("check_in_attendees")],
    )
    def check_in_ticket(self, event_id: UUID, ticket_id: UUID) -> models.Ticket:
        """Check in an attendee by scanning their ticket."""
        event = self.get_one(event_id)
        return check_in_ticket(event, ticket_id, self.user())

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

    # RSVP Admin Endpoints

    @route.get(
        "/rsvps",
        url_name="list_rsvps",
        response=PaginatedResponseSchema[schema.RSVPDetailSchema],
        permissions=[EventPermission("invite_to_event")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["user__email", "user__first_name", "user__last_name", "user__preferred_name"])
    def list_rsvps(
        self,
        event_id: UUID,
        params: filters.RSVPFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.EventRSVP]:
        """List all RSVPs for an event.

        Shows all users who have RSVPed to the event with their status.
        Use this to see who is attending, not attending, or maybe attending.
        Supports filtering by status and user_id.
        """
        event = self.get_one(event_id)
        qs = models.EventRSVP.objects.select_related("user").filter(event=event).order_by("-created_at")
        return params.filter(qs).distinct()

    @route.get(
        "/rsvps/{rsvp_id}",
        url_name="get_rsvp",
        response=schema.RSVPDetailSchema,
        permissions=[EventPermission("invite_to_event")],
        throttle=UserDefaultThrottle(),
    )
    def get_rsvp(self, event_id: UUID, rsvp_id: UUID) -> models.EventRSVP:
        """Get details of a specific RSVP."""
        event = self.get_one(event_id)
        return get_object_or_404(models.EventRSVP.objects.select_related("user"), pk=rsvp_id, event=event)

    @route.post(
        "/rsvps",
        url_name="create_rsvp",
        response=schema.RSVPDetailSchema,
        permissions=[EventPermission("invite_to_event")],
    )
    def create_rsvp(self, event_id: UUID, payload: schema.RSVPCreateSchema) -> models.EventRSVP:
        """Create an RSVP on behalf of a user.

        Use this when a user contacts the organization to RSVP outside the platform
        (e.g., via text, email, or in person).
        """
        event = self.get_one(event_id)

        # Verify user exists
        user = get_object_or_404(RevelUser, pk=payload.user_id)

        # Create or update RSVP (due to unique constraint on event+user)
        rsvp, created = models.EventRSVP.objects.update_or_create(
            event=event, user=user, defaults={"status": payload.status}
        )

        return rsvp

    @route.put(
        "/rsvps/{rsvp_id}",
        url_name="update_rsvp",
        response=schema.RSVPDetailSchema,
        permissions=[EventPermission("invite_to_event")],
    )
    def update_rsvp(self, event_id: UUID, rsvp_id: UUID, payload: schema.RSVPUpdateSchema) -> models.EventRSVP:
        """Update an existing RSVP.

        Use this to change a user's RSVP status when they contact you to update their response.
        """
        event = self.get_one(event_id)
        rsvp = get_object_or_404(models.EventRSVP, pk=rsvp_id, event=event)
        return update_db_instance(rsvp, payload)

    @route.delete(
        "/rsvps/{rsvp_id}",
        url_name="delete_rsvp",
        response={204: None},
        permissions=[EventPermission("invite_to_event")],
    )
    def delete_rsvp(self, event_id: UUID, rsvp_id: UUID) -> tuple[int, None]:
        """Delete an RSVP.

        Use this to remove a user's RSVP entirely from the event.
        Note: This is different from setting status to "no" - it completely removes the RSVP record.
        """
        event = self.get_one(event_id)
        rsvp = get_object_or_404(models.EventRSVP, pk=rsvp_id, event=event)
        rsvp.delete()
        return 204, None

    # Waitlist Management

    @route.get(
        "/waitlist",
        url_name="list_waitlist",
        response=PaginatedResponseSchema[schema.WaitlistEntrySchema],
        permissions=[EventPermission("invite_to_event")],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["user__email", "user__first_name", "user__last_name", "user__preferred_name"])
    def list_waitlist(
        self,
        event_id: UUID,
    ) -> QuerySet[models.EventWaitList]:
        """List all users on the event waitlist.

        Shows users waiting for spots to become available, ordered by join time (FIFO).
        Use this to see who is waiting and manage the waitlist.
        """
        event = self.get_one(event_id)
        return models.EventWaitList.objects.select_related("user").filter(event=event).order_by("created_at")

    @route.delete(
        "/waitlist/{waitlist_id}",
        url_name="delete_waitlist_entry",
        response={204: None},
        permissions=[EventPermission("invite_to_event")],
    )
    def delete_waitlist_entry(self, event_id: UUID, waitlist_id: UUID) -> tuple[int, None]:
        """Remove a user from the event waitlist.

        Use this to manually remove someone from the waitlist (e.g., if they requested removal
        or if you want to manage the list manually).
        """
        event = self.get_one(event_id)
        waitlist_entry = get_object_or_404(models.EventWaitList, pk=waitlist_id, event=event)
        waitlist_entry.delete()
        return 204, None
