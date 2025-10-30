import typing as t
from uuid import UUID

from django.db.models import QuerySet
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Query
from ninja.errors import HttpError
from ninja_extra import (
    api_controller,
    route,
)
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching
from ninja_jwt.authentication import JWTAuth

from accounts.models import RevelUser
from common.authentication import OptionalAuth
from common.schema import ResponseMessage
from common.throttling import QuestionnaireSubmissionThrottle, WriteThrottle
from events import filters, models, schema
from events.service import event_service
from events.service.event_manager import EventManager, EventUserEligibility
from questionnaires.models import Questionnaire, QuestionnaireSubmission
from questionnaires.schema import (
    QuestionnaireSchema,
    QuestionnaireSubmissionOrEvaluationSchema,
    QuestionnaireSubmissionResponseSchema,
    QuestionnaireSubmissionSchema,
)
from questionnaires.service import QuestionnaireService
from questionnaires.tasks import evaluate_questionnaire_submission

from .permissions import CanPurchaseTicket
from .user_aware_controller import UserAwareController


@api_controller("/events", auth=OptionalAuth(), tags=["Events"])
class EventController(UserAwareController):
    def get_queryset(self, include_past: bool = False) -> models.event.EventQuerySet:
        """Get the queryset based on the user."""
        allowed_ids: list[UUID] = []
        if et := self.get_event_token():
            allowed_ids = [et.event_id]
        return models.Event.objects.for_user(self.maybe_user(), include_past=include_past, allowed_ids=allowed_ids)

    def get_one(self, event_id: UUID) -> models.Event:
        """Wrapper helper."""
        return t.cast(
            models.Event,
            self.get_object_or_exception(self.get_queryset(include_past=True).with_organization(), pk=event_id),
        )

    def get_event_token(self) -> models.EventToken | None:
        """Get an event token from X-Event-Token header or et query param (legacy).

        Preferred: X-Event-Token header
        Legacy: ?et= query parameter (for backwards compatibility)
        """
        token = (
            self.context.request.META.get("HTTP_X_EVENT_TOKEN")  # type: ignore[union-attr]
            or self.context.request.GET.get("et")  # type: ignore[union-attr]
        )
        if token:
            return event_service.get_event_token(token)
        return None

    def get_questionnaire_service(self, questionnaire_id: UUID) -> QuestionnaireService:
        """Get the questionnaire for this request."""
        try:
            service = QuestionnaireService(questionnaire_id)
        except Questionnaire.DoesNotExist:
            raise Http404()
        return service

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
        By default shows only upcoming events; set include_past=true to see past events.
        Ordering: 'distance' (default) shows nearest events based on user location, 'start' shows
        soonest first, '-start' shows latest first. Supports filtering by organization, series,
        tags, and text search.
        """
        qs = params.filter(self.get_queryset(include_past=include_past or params.past_events is True)).distinct()
        if order_by == "distance":
            return event_service.order_by_distance(self.user_location(), qs)
        return qs.order_by(order_by)

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
        Frontend extracts tokens from shareable URLs like `/events/{event_id}?et={token_id}`
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
        return 404, ResponseMessage(message="Token not found or expired.")

    @route.post(
        "/claim-invitation/{token}",
        url_name="event_claim_invitation",
        response={200: schema.MinimalEventSchema, 400: ResponseMessage},
        auth=JWTAuth(),
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
        return 400, ResponseMessage(message="The token is invalid or expired.")

    @route.get(
        "/me/invitation-requests",
        url_name="list_my_invitation_requests",
        response=PaginatedResponseSchema[schema.EventInvitationRequestSchema],
        auth=JWTAuth(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["event__name", "event__description", "message"])
    def list_my_invitation_requests(
        self,
        event_id: UUID | None = None,
        params: filters.InvitationRequestFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.EventInvitationRequest]:
        """View your invitation requests across all events.

        Returns your invitation requests with their current status. By default shows only pending
        requests; use ?status=approved or ?status=rejected to see decided requests, or omit the
        status parameter to see all requests. Filter by event_id to see requests for a specific
        event. Use this to track which events you've requested access to.
        """
        qs = models.EventInvitationRequest.objects.select_related("event").filter(user=self.user())
        if event_id:
            qs = qs.filter(event_id=event_id)
        return params.filter(qs).distinct()

    @route.get(
        "/me/my-invitations",
        url_name="list_my_invitations",
        response=PaginatedResponseSchema[schema.MyEventInvitationSchema],
        auth=JWTAuth(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["event__name", "event__description", "custom_message"])
    def list_my_invitations(
        self,
        event_id: UUID | None = None,
        include_past: bool = False,
    ) -> QuerySet[models.EventInvitation]:
        """View your event invitations across all events.

        Returns invitations you've received with event details and any special privileges granted
        (tier assignment, waived requirements, etc.). By default shows only invitations for upcoming
        events; set include_past=true to include past events. An event is considered past if its end
        time has passed. Filter by event_id to see invitations for a specific event.
        """
        qs = models.EventInvitation.objects.select_related("event", "tier").filter(user=self.user())

        if event_id:
            qs = qs.filter(event_id=event_id)

        if not include_past:
            # Filter for upcoming events: end > now
            qs = qs.filter(event__end__gt=timezone.now())

        return qs.distinct().order_by("-created_at")

    @route.get(
        "/me/my-tickets",
        url_name="list_user_tickets",
        response=PaginatedResponseSchema[schema.UserTicketSchema],
        auth=JWTAuth(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["event__name", "event__description", "tier__name"])
    def list_user_tickets(
        self,
        params: filters.TicketFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.Ticket]:
        """View your tickets across all events.

        Returns all your tickets with their current status and event details.
        By default, shows only tickets for upcoming events; set include_past=true
        to include past events. An event is considered past if its end time has passed.
        Supports filtering by status (pending/active/cancelled/checked_in) and
        payment method. Results are ordered by newest first.
        """
        qs = models.Ticket.objects.select_related("event", "tier").filter(user=self.user()).order_by("-created_at")
        return params.filter(qs).distinct()

    @route.get(
        "/{event_id}/attendee-list",
        url_name="event_attendee_list",
        response=PaginatedResponseSchema[schema.AttendeeSchema],
        auth=JWTAuth(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def get_event_attendees(self, event_id: UUID) -> QuerySet[RevelUser]:
        """Get the list of confirmed attendees for this event.

        Returns users who have RSVPed 'yes' or have active tickets. Visibility is controlled by
        event settings - attendee lists may be hidden from regular attendees. Organization staff
        and event creators always have access.
        """
        event = self.get_one(event_id)
        return event.attendees(self.user()).distinct()

    @route.get(
        "/{event_id}/my-status",
        url_name="get_my_event_status",
        response=schema.EventUserStatusSchema | EventUserEligibility,
        auth=JWTAuth(),
    )
    def get_my_event_status(self, event_id: UUID) -> schema.EventUserStatusSchema | EventUserEligibility:
        """Check the authenticated user's current status and eligibility for an event.

        Returns either the user's RSVP/ticket status if they've already joined, or an eligibility
        check result explaining what steps are needed to attend. The eligibility check validates:
        event status, RSVP deadline, invitations, organization membership, required questionnaires,
        capacity limits, and ticket availability. Use this to determine which action to show users
        (RSVP button, buy ticket, fill questionnaire, etc.).
        """
        event = self.get_one(event_id)
        if (
            ticket := models.Ticket.objects.select_related("tier").filter(event=event, user_id=self.user().id).first()
        ) and event.requires_ticket:
            return schema.EventTicketSchema.from_orm(ticket)
        elif rsvp := models.EventRSVP.objects.filter(event=event, user_id=self.user().id).first():
            return schema.EventRSVPSchema.from_orm(rsvp)
        return EventManager(self.user(), event).check_eligibility()

    @route.post(
        "/{event_id}/invitation-requests",
        url_name="create_invitation_request",
        response={201: schema.EventInvitationRequestSchema},
        auth=JWTAuth(),
        throttle=WriteThrottle(),
    )
    def create_invitation_request(
        self, event_id: UUID, payload: schema.EventInvitationRequestCreateSchema
    ) -> tuple[int, models.EventInvitationRequest]:
        """Submit a request to be invited to a private or invite-only event.

        Creates an invitation request that event organizers can approve or reject. Include an
        optional message explaining why you want to attend. Returns 400 if you've already
        submitted a request for this event. Check GET /{event_id}/my-status to see if you
        need an invitation.
        """
        event = self.get_one(event_id)
        return 201, event_service.create_invitation_request(event, self.user(), message=payload.message)

    @route.get(
        "/{event_id}/resources",
        url_name="list_event_resources",
        response=PaginatedResponseSchema[schema.AdditionalResourceSchema],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["name", "description"])
    def list_resources(
        self,
        event_id: UUID,
        params: filters.ResourceFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.AdditionalResource]:
        """Get supplementary resources attached to this event.

        Returns resources like documents, links, or media files provided by event organizers.
        Resources may be public or restricted to attendees only. Supports filtering by type
        (file, link, etc.) and text search.
        """
        event = self.get_one(event_id)
        qs = models.AdditionalResource.objects.for_user(self.maybe_user()).filter(events=event).with_related()
        return params.filter(qs).distinct()

    @route.delete(
        "/invitation-requests/{request_id}",
        url_name="delete_invitation_request",
        response={204: None},
        auth=JWTAuth(),
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

    @route.get("/{org_slug}/{event_slug}", url_name="get_event_by_slug", response=schema.EventDetailSchema)
    def get_event_by_slugs(self, org_slug: str, event_slug: str) -> models.Event:
        """Retrieve event details using human-readable organization and event slugs.

        Use this for clean URLs like /events/tech-meetup/monthly-session. Returns 404 if
        the event doesn't exist or you don't have permission to view it.
        """
        return t.cast(
            models.Event,
            self.get_object_or_exception(self.get_queryset(), slug=event_slug, organization__slug=org_slug),
        )

    @route.get("/{event_id}", url_name="get_event", response=schema.EventDetailSchema)
    def get_event(self, event_id: UUID) -> models.Event:
        """Retrieve full event details by ID.

        Returns comprehensive event information including description, location, times, organization,
        ticket tiers, and visibility settings. Use this to display the event detail page.
        """
        return self.get_one(event_id)

    @route.post(
        "/{event_id}/rsvp/{answer}",
        url_name="rsvp_event",
        response={200: schema.EventRSVPSchema, 400: EventUserEligibility},
        auth=JWTAuth(),
        throttle=WriteThrottle(),
    )
    def rsvp_event(self, event_id: UUID, answer: models.EventRSVP.Status) -> models.EventRSVP:
        """RSVP to a non-ticketed event (answer: 'yes', 'no', or 'maybe').

        Only works for events where requires_ticket=false. Runs full eligibility check including
        event status, RSVP deadline, invitations, membership requirements, required questionnaires,
        and capacity limits. Returns RSVP record on success. On failure, returns eligibility details
        explaining what's blocking you and what next_step to take (e.g., complete questionnaire,
        request invitation).
        """
        event = self.get_one(event_id)
        manager = EventManager(self.user(), event)
        return manager.rsvp(answer)

    @route.get(
        "/{event_id}/tickets/tiers",
        url_name="tier_list",
        response={200: list[schema.TierSchema]},
    )
    def list_tiers(self, event_id: UUID) -> models.event.TicketTierQuerySet:
        """Get all ticket tiers available for purchase at this event.

        Returns ticket types with pricing, availability, and sales windows. Filters tiers based
        on user eligibility - you'll only see tiers you're allowed to purchase. Check visibility
        settings and sales_start_at/sales_end_at to determine which are currently on sale.
        """
        event = self.get_one(event_id)
        return models.TicketTier.objects.for_user(self.user()).filter(event=event).distinct()

    @route.post(
        "/{event_id}/tickets/{tier_id}/checkout",
        url_name="ticket_checkout",
        response={200: schema.StripeCheckoutSessionSchema | schema.EventTicketSchema, 400: EventUserEligibility},
        auth=JWTAuth(),
        throttle=WriteThrottle(),
        permissions=[CanPurchaseTicket()],
    )
    def ticket_checkout(
        self,
        event_id: UUID,
        tier_id: UUID,
    ) -> schema.StripeCheckoutSessionSchema | schema.EventTicketSchema:
        """Purchase a fixed-price event ticket.

        Runs eligibility checks before allowing purchase. For online payment: returns Stripe
        checkout URL to redirect user for payment. For free/offline/at-the-door tickets: creates
        ticket immediately and returns it. Cannot be used for pay-what-you-can (PWYC) tiers -
        use POST /{event_id}/tickets/{tier_id}/checkout/pwyc instead. On eligibility failure,
        returns 400 with eligibility details explaining what's blocking you and what next_step
        to take (e.g., complete questionnaire, request invitation, wait for tickets to go on sale).
        """
        # Note: calling get one will cause to call Event.for_user();
        # then TicketTier.for_user() will call Event.for_user() as well.
        # This is convenient from a code flow perspective but maybe not the best performance wise
        event = get_object_or_404(self.get_queryset(include_past=True), pk=event_id)
        tier = get_object_or_404(
            models.TicketTier.objects.for_user(self.user()),
            pk=tier_id,
            event=event,
        )
        if tier.price_type == models.TicketTier.PriceType.PWYC:
            raise HttpError(400, "Ticket price type PWYC")
        manager = EventManager(self.user(), event)
        ticket_or_url = manager.create_ticket(tier)
        if isinstance(ticket_or_url, models.Ticket):
            return schema.EventTicketSchema.from_orm(ticket_or_url)
        return schema.StripeCheckoutSessionSchema(checkout_url=ticket_or_url)

    @route.post(
        "/{event_id}/tickets/{tier_id}/checkout/pwyc",
        url_name="ticket_pwyc_checkout",
        response={200: schema.StripeCheckoutSessionSchema | schema.EventTicketSchema, 400: EventUserEligibility},
        auth=JWTAuth(),
        throttle=WriteThrottle(),
        permissions=[CanPurchaseTicket()],
    )
    def ticket_pwyc_checkout(
        self,
        event_id: UUID,
        tier_id: UUID,
        payload: schema.PWYCCheckoutPayloadSchema,
    ) -> schema.StripeCheckoutSessionSchema | schema.EventTicketSchema:
        """Purchase a pay-what-you-can (PWYC) ticket with a user-specified amount.

        Only works for ticket tiers with price_type=PWYC. Validates the amount is within the
        tier's min/max bounds. Returns Stripe checkout URL for online payment, or creates ticket
        immediately for free/offline payment methods. Returns 400 for non-PWYC tiers, if amount
        is out of bounds, or on eligibility failure (with eligibility details explaining what's
        blocking you and what next_step to take).
        """
        event = get_object_or_404(self.get_queryset(include_past=True), pk=event_id)
        tier = get_object_or_404(
            models.TicketTier.objects.for_user(self.user()),
            pk=tier_id,
            event=event,
        )

        # Validate that this tier is actually PWYC
        if tier.price_type != models.TicketTier.PriceType.PWYC:
            raise HttpError(400, "This endpoint is only for pay-what-you-can tickets")

        # Validate PWYC amount is within bounds
        if payload.pwyc < tier.pwyc_min:
            raise HttpError(400, f"PWYC amount must be at least {tier.pwyc_min}")

        if tier.pwyc_max and payload.pwyc > tier.pwyc_max:
            raise HttpError(400, f"PWYC amount must be at most {tier.pwyc_max}")

        manager = EventManager(self.user(), event)
        ticket_or_url = manager.create_ticket(tier, price_override=payload.pwyc)
        if isinstance(ticket_or_url, models.Ticket):
            return schema.EventTicketSchema.from_orm(ticket_or_url)
        return schema.StripeCheckoutSessionSchema(checkout_url=ticket_or_url)

    @route.get(
        "/{event_id}/questionnaire/{questionnaire_id}", url_name="get_questionnaire", response=QuestionnaireSchema
    )
    def get_questionnaire(self, event_id: UUID, questionnaire_id: UUID) -> QuestionnaireSchema:
        """Retrieve a questionnaire required for event admission.

        Returns the questionnaire structure with all sections and questions. Questions may be
        shuffled based on questionnaire settings. Use this to display the form that users must
        complete before accessing the event.
        """
        self.get_one(event_id)
        # todo: verify that the questionnaire belongs to the event
        questionnaire_service = self.get_questionnaire_service(questionnaire_id)
        return questionnaire_service.build()

    @route.post(
        "/{event_id}/questionnaire/{questionnaire_id}/submit",
        url_name="submit_questionnaire",
        response={200: QuestionnaireSubmissionOrEvaluationSchema, 400: ResponseMessage},
        auth=JWTAuth(),
        throttle=QuestionnaireSubmissionThrottle(),
    )
    def submit_questionnaire(
        self, event_id: UUID, questionnaire_id: UUID, submission: QuestionnaireSubmissionSchema
    ) -> QuestionnaireSubmissionOrEvaluationSchema:
        """Submit answers to an event admission questionnaire.

        Validates all required questions are answered. If submission status is 'ready', triggers
        automatic evaluation (may use LLM for free-text answers). Depending on the questionnaire's
        evaluation_mode (automatic/manual/hybrid), results may be immediate or pending staff review.
        Passing the questionnaire may be required before you can RSVP or purchase tickets.
        """
        self.get_one(event_id)
        questionnaire_service = self.get_questionnaire_service(questionnaire_id)
        db_submission = questionnaire_service.submit(self.user(), submission)
        if submission.status == QuestionnaireSubmission.Status.READY:
            evaluate_questionnaire_submission.delay(str(db_submission.pk))
        return QuestionnaireSubmissionResponseSchema.from_orm(db_submission)
