import typing as t
from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja import Query
from ninja_extra import (
    api_controller,
    route,
)
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.authentication import I18nJWTAuth, OptionalAuth
from common.schema import ResponseMessage
from common.throttling import WriteThrottle
from events import filters, models, schema
from events.service import event_service, stripe_service
from events.service import guest as guest_service

from .base import EventPublicBaseController


@api_controller("/events", auth=OptionalAuth(), tags=["Events"])
class EventPublicDiscoveryController(EventPublicBaseController):
    """Handles event discovery, listing, and non-event_id routes.

    IMPORTANT: This controller contains all non-event_id routes to ensure they are
    matched BEFORE the /{uuid:event_id} catch-all routes in other controllers.
    """

    @route.get("/", url_name="list_events", response=PaginatedResponseSchema[schema.EventInListSchema])
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(
        Searching,
        search_fields=[
            "name",
            "description",
            "event_series__name",
            "event_series__description",
            "organization__name",
            "organization__description",
            "tags__tag__name",
        ],
    )
    def list_events(
        self,
        params: filters.EventFilterSchema = Query(...),  # type: ignore[type-arg]
        order_by: t.Literal["start", "-start", "distance"] = "distance",
        include_past: bool = False,
    ) -> QuerySet[models.Event]:
        """Browse and search events visible to the current user.

        Results are filtered by visibility rules (public/private), event status, and user permissions.
        By default, shows only upcoming events; set include_past=true to see past events.
        Ordering: 'distance' (default) shows nearest events based on user location, 'start' shows
        soonest first, '-start' shows latest first. Supports filtering by organization, series,
        tags, and text search.
        """
        params.next_events = not include_past
        qs = params.filter(self.get_queryset(include_past=include_past or params.past_events is True)).distinct()
        if order_by == "distance":
            return event_service.order_by_distance(self.user_location(), qs)
        return qs.order_by(order_by)

    @route.get("/calendar", url_name="calendar_events", response=list[schema.EventInListSchema])
    def calendar_events(
        self,
        params: filters.EventFilterSchema = Query(...),  # type: ignore[type-arg]
        calendar_params: filters.CalendarParamsSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.Event]:
        """Get events for a calendar view (week, month, or year).

        Returns a flat list of events for the specified time period. If no time parameters are
        provided, defaults to the current month.

        **Time Parameters:**
        - `week`: ISO week number (1-53) - uses current year if year parameter not provided.
        - `month`: Month number (1-12) - uses current year if year parameter not provided.
        - `year`: Year (e.g., 2025) - returns all events in that year if month/week not specified.

        **Examples:**
        - `/calendar` - Current month's events
        - `/calendar?month=12&year=2025` - December 2025 events
        - `/calendar?week=1&year=2025` - Week 1 of 2025
        - `/calendar?year=2025` - All 2025 events

        **Additional Filters:**
        Supports all EventFilterSchema filters (organization, tags, event_type, etc.).
        Note: The `next_events` filter is disabled by default for calendar views since the date
        range is explicitly specified. Use `start_after` or `start_before` to filter events
        within the calendar's date range if needed.

        Results are ordered by start time ascending.
        """
        start_datetime, end_datetime = event_service.calculate_calendar_date_range(**calendar_params.model_dump())
        qs = self.get_queryset(include_past=True).filter(start__gte=start_datetime, start__lt=end_datetime)
        # Disable next_events default filter for calendar views since date range is explicit.
        # Users can still filter using start_after/start_before if needed.
        params.next_events = None
        qs = params.filter(qs).distinct()
        return qs.order_by("start")

    @route.get(
        "/tokens/{token_id}",
        url_name="get_event_token",
        response={200: schema.EventTokenSchema, 404: ResponseMessage},
    )
    def get_event_token_details(self, token_id: str) -> tuple[int, models.EventToken | ResponseMessage]:
        """Preview an event token to see what access it grants.

        This endpoint allows users to see token details before deciding whether to claim it.
        No authentication required - tokens are meant to be shareable.

        **Primary Use Case: Visibility via Token Header**
        The main purpose of event tokens is to grant temporary visibility to events.
        Frontend extracts tokens from shareable URLs like `/events/{uuid:event_id}?et={token_id}`
        and passes them to the API via the `X-Event-Token` header.

        **Returns:**
        - `id`: The token code (for use in URLs as `?et=` query param)
        - `event`: The event this token grants access to
        - `name`: Display name (e.g., "Instagram Followers Link")
        - `expires_at`: When the token stops working (null = never expires)
        - `max_uses`: Maximum number of claims (0 = unlimited)
        - `uses`: Current number of claims
        - `grants_invitation`: Whether users can claim invitations with this token
        - `ticket_tier`: Which ticket tier users get when claiming (null if no invitation)
        - `invitation_payload`: Custom invitation metadata (null if no invitation)

        **Frontend Usage:**
        ```javascript
        // When user visits /events/123?et=abc123, extract and use the token:
        const urlParams = new URLSearchParams(window.location.search);
        const eventToken = urlParams.get('et');

        // Preview the token first
        const token = await fetch(`/api/events/tokens/${eventToken}`).then(r => r.json());

        // Then access the event with token in header
        const event = await fetch(`/api/events/123`, {
          headers: { 'X-Event-Token': eventToken }
        }).then(r => r.json());

        if (token.grants_invitation) {
          // This token can be claimed for an invitation
          showClaimButton(`You can join: ${event.name}`);
        } else {
          // This is a read-only token for viewing only
          showMessage(`View access to: ${event.name}`);
        }
        ```

        **Token Types:**
        1. **Read-Only Tokens** (`grants_invitation=False`, `invitation_payload=null`)
           - Share event link with non-members
           - Users can VIEW the event but cannot automatically join
           - Example: Share in group chat so members can see event details

        2. **Invitation Tokens** (`grants_invitation=True` with `invitation_payload`)
           - Users can both VIEW and CLAIM an invitation
           - Creates EventInvitation when claimed via POST `/events/claim-invitation/{token}`
           - Optional ticket tier auto-assignment

        **Error Cases:**
        - 404: Token doesn't exist or has been deleted
        """
        if token := event_service.get_event_token(token_id):
            return 200, token
        return 404, ResponseMessage(message=str(_("Token not found or expired.")))

    @route.post(
        "/claim-invitation/{token}",
        url_name="event_claim_invitation",
        response={200: schema.MinimalEventSchema, 400: ResponseMessage},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def claim_invitation(self, token: str) -> tuple[int, models.Event | ResponseMessage]:
        """Accept an event invitation using a token from an invitation link or email.

        Creates an EventInvitation record for the user, granting access to the event.
        Invitations can bypass certain eligibility requirements like membership, capacity limits,
        and RSVP deadlines. Returns the event on success, or 400 if the token is invalid/expired.
        """
        if invitation := event_service.claim_invitation(self.user(), token):
            return 200, invitation.event
        return 400, ResponseMessage(message=str(_("The token is invalid or expired.")))

    @route.delete(
        "/invitation-requests/{request_id}",
        url_name="delete_invitation_request",
        response={204: None},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def delete_invitation_request(self, request_id: UUID) -> tuple[int, None]:
        """Cancel a pending invitation request.

        Withdraws your invitation request for an event. Only works for your own requests
        that haven't been decided yet. Returns 404 if the request doesn't exist or doesn't
        belong to you.
        """
        invitation_request = get_object_or_404(models.EventInvitationRequest, pk=request_id, user_id=self.user().id)
        invitation_request.delete()
        return 204, None

    @route.post(
        "/guest-actions/confirm",
        url_name="confirm_guest_action",
        response={
            200: schema.EventRSVPSchema | schema.BatchCheckoutResponse,
            400: ResponseMessage,
        },
        throttle=WriteThrottle(),
    )
    def confirm_guest_action(
        self, payload: schema.GuestActionConfirmSchema
    ) -> schema.EventRSVPSchema | schema.BatchCheckoutResponse:
        """Confirm a guest action (RSVP or ticket purchase) via JWT token from email.

        Validates the token, executes the action (creates RSVP or ticket), and blacklists the token
        to prevent reuse. Returns the created RSVP or BatchCheckoutResponse with tickets on success.
        Returns 400 if token is invalid, expired, already used, or if eligibility checks fail (e.g., event became full).
        """
        return guest_service.confirm_guest_action(payload.token)

    # ---- Checkout Management Endpoints ----
    # These must be defined BEFORE /{uuid:event_id} routes to avoid path conflicts

    @route.get(
        "/checkout/{payment_id}/resume",
        url_name="resume_checkout",
        response={200: schema.StripeCheckoutSessionSchema, 404: ResponseMessage},
        auth=I18nJWTAuth(),
    )
    def resume_checkout(
        self,
        payment_id: UUID,
    ) -> schema.StripeCheckoutSessionSchema:
        """Resume a pending Stripe checkout session.

        Returns the Stripe checkout URL for an existing pending payment. Use this when a user
        started a checkout but didn't complete payment (e.g., closed the browser, session timeout).

        The payment_id can be obtained from the ticket's payment field via GET /{uuid:event_id}/my-status.

        If the checkout session has expired, cleans up the pending tickets and returns 404.
        The user should then start a new purchase via the /checkout endpoint.
        """
        checkout_url = stripe_service.resume_pending_checkout(str(payment_id), self.user())
        return schema.StripeCheckoutSessionSchema(checkout_url=checkout_url)

    @route.delete(
        "/checkout/{payment_id}/cancel",
        url_name="cancel_checkout",
        response={200: ResponseMessage, 400: ResponseMessage, 404: ResponseMessage},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def cancel_checkout(
        self,
        payment_id: UUID,
    ) -> ResponseMessage:
        """Cancel a pending Stripe checkout and delete associated tickets.

        Cancels the pending payment and deletes all tickets in the same batch. Use this when
        the user wants to abandon their cart and start fresh.

        The payment_id can be obtained from the ticket's payment field via GET /{uuid:event_id}/my-status.

        Only works for PENDING payments. Once a payment is completed, use the refund flow instead.
        """
        tickets_cancelled = stripe_service.cancel_pending_checkout(str(payment_id), self.user())
        return ResponseMessage(message=f"{tickets_cancelled} ticket(s) cancelled successfully.")
