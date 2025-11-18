import typing as t
from uuid import UUID

from django.db.models import QuerySet
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja import Query
from ninja.errors import HttpError
from ninja_extra import (
    api_controller,
    route,
)
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from accounts.models import RevelUser
from common.authentication import I18nJWTAuth, OptionalAuth
from common.controllers import UserAwareController
from common.schema import ResponseMessage
from common.throttling import QuestionnaireSubmissionThrottle, WriteThrottle
from events import filters, models, schema
from events.service import event_service
from events.service import guest as guest_service
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


@api_controller("/events", auth=OptionalAuth(), tags=["Events"])
class EventController(UserAwareController):
    def get_queryset(self, include_past: bool = False, full: bool = True) -> models.event.EventQuerySet:
        """Get the queryset based on the user."""
        allowed_ids: list[UUID] = []
        if et := self.get_event_token():
            allowed_ids = [et.event_id]
        qs = models.Event.objects.for_user(self.maybe_user(), include_past=include_past, allowed_ids=allowed_ids)
        if not full:
            return qs
        return models.Event.objects.full().for_user(
            self.maybe_user(), include_past=include_past, allowed_ids=allowed_ids
        )

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

    @route.get(
        "/{event_id}/attendee-list",
        url_name="event_attendee_list",
        response=PaginatedResponseSchema[schema.AttendeeSchema],
        auth=I18nJWTAuth(),
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
        auth=I18nJWTAuth(),
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
        auth=I18nJWTAuth(),
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

    @route.get(
        "/{event_id}/dietary-summary",
        url_name="event_dietary_summary",
        response=schema.EventDietarySummarySchema,
        auth=I18nJWTAuth(),
    )
    def get_dietary_summary(self, event_id: UUID) -> schema.EventDietarySummarySchema:
        """Get aggregated dietary restrictions and preferences for event attendees.

        Returns de-identified, aggregated dietary information to help with meal planning for events
        and potlucks. Event organizers/staff see all dietary data (public + private). Regular attendees
        only see data marked as public by other attendees. Data includes counts of restrictions/preferences
        and non-empty notes/comments, but no user associations for privacy.
        """
        event = self.get_one(event_id)
        return event_service.get_event_dietary_summary(event, self.user())

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
        response={200: schema.EventRSVPSchema | schema.EventTicketSchema, 400: ResponseMessage},
        throttle=WriteThrottle(),
    )
    def confirm_guest_action(
        self, payload: schema.GuestActionConfirmSchema
    ) -> schema.EventRSVPSchema | schema.EventTicketSchema:
        """Confirm a guest action (RSVP or ticket purchase) via JWT token from email.

        Validates the token, executes the action (creates RSVP or ticket), and blacklists the token
        to prevent reuse. Returns the created RSVP or ticket on success. Returns 400 if token is
        invalid, expired, already used, or if eligibility checks fail (e.g., event became full).
        """
        return guest_service.confirm_guest_action(payload.token)

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
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def rsvp_event(self, event_id: UUID, answer: models.EventRSVP.RsvpStatus) -> models.EventRSVP:
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

    @route.post(
        "/{event_id}/waitlist/join",
        url_name="join_waitlist",
        response={200: ResponseMessage, 400: ResponseMessage},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def join_waitlist(self, event_id: UUID) -> ResponseMessage:
        """Join the waitlist for a full event.

        Allows users to join the event waitlist when the event is at capacity. Users will be
        notified when spots become available. Returns 400 if the event doesn't have an open
        waitlist or if the user is already on the waitlist.
        """
        event = self.get_one(event_id)

        # Check if waitlist is open
        if not event.waitlist_open:
            raise HttpError(400, str(_("This event does not have an open waitlist.")))

        # Use get_or_create to handle duplicate joins
        waitlist_entry, created = models.EventWaitList.objects.get_or_create(
            event=event,
            user=self.user(),
        )

        # If entry already existed, inform the user
        if not created:
            return ResponseMessage(message=str(_("You are already on the waitlist for this event.")))

        return ResponseMessage(message=str(_("Successfully joined the waitlist.")))

    @route.delete(
        "/{event_id}/waitlist/leave",
        url_name="leave_waitlist",
        response={200: ResponseMessage, 400: ResponseMessage},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def leave_waitlist(self, event_id: UUID) -> ResponseMessage:
        """Leave the waitlist for a full event.

        Allows users to leave the event waitlist.
        """
        event = self.get_one(event_id)

        # Check if waitlist is open
        if not event.waitlist_open:
            raise HttpError(400, str(_("This event does not have an open waitlist.")))

        # Remove the user from the waitlist if they're on it
        models.EventWaitList.objects.filter(
            event=event,
            user=self.user(),
        ).delete()
        return ResponseMessage(message=str(_("Successfully left the waitlist.")))

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
        return models.TicketTier.objects.for_user(self.maybe_user()).filter(event=event).distinct()

    @route.post(
        "/{event_id}/tickets/{tier_id}/checkout",
        url_name="ticket_checkout",
        response={200: schema.StripeCheckoutSessionSchema | schema.EventTicketSchema, 400: EventUserEligibility},
        auth=I18nJWTAuth(),
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
            raise HttpError(400, str(_("Ticket price type PWYC")))
        manager = EventManager(self.user(), event)
        ticket_or_url = manager.create_ticket(tier)
        if isinstance(ticket_or_url, models.Ticket):
            return schema.EventTicketSchema.from_orm(ticket_or_url)
        return schema.StripeCheckoutSessionSchema(checkout_url=ticket_or_url)

    @route.post(
        "/{event_id}/tickets/{tier_id}/checkout/pwyc",
        url_name="ticket_pwyc_checkout",
        response={200: schema.StripeCheckoutSessionSchema | schema.EventTicketSchema, 400: EventUserEligibility},
        auth=I18nJWTAuth(),
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
            raise HttpError(400, str(_("This endpoint is only for pay-what-you-can tickets")))

        # Validate PWYC amount is within bounds
        if payload.pwyc < tier.pwyc_min:
            raise HttpError(400, str(_("PWYC amount must be at least {min_amount}")).format(min_amount=tier.pwyc_min))

        if tier.pwyc_max and payload.pwyc > tier.pwyc_max:
            raise HttpError(400, str(_("PWYC amount must be at most {max_amount}")).format(max_amount=tier.pwyc_max))

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
        auth=I18nJWTAuth(),
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
        if submission.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY:
            evaluate_questionnaire_submission.delay(str(db_submission.pk))
        return QuestionnaireSubmissionResponseSchema.from_orm(db_submission)

    # ---- Guest User Endpoints (No Authentication Required) ----

    @route.post(
        "/{event_id}/rsvp/{answer}/public",
        url_name="guest_rsvp",
        response={200: schema.GuestActionResponseSchema, 400: ResponseMessage},
        throttle=WriteThrottle(),
    )
    def guest_rsvp(
        self, event_id: UUID, answer: models.EventRSVP.RsvpStatus, payload: schema.GuestUserDataSchema
    ) -> schema.GuestActionResponseSchema:
        """RSVP to an event without authentication (guest user).

        Creates or updates a guest user and sends a confirmation email. The RSVP is created only
        after the user confirms via the email link. Requires event.can_attend_without_login=True.
        Returns 400 if event doesn't allow guest access or if a non-guest account exists with
        the provided email.
        """
        self.ensure_not_authenticated()
        event = self.get_one(event_id)
        return guest_service.handle_guest_rsvp(event, answer, payload.email, payload.first_name, payload.last_name)

    @route.post(
        "/{event_id}/tickets/{tier_id}/checkout/public",
        url_name="guest_ticket_checkout",
        response={200: schema.StripeCheckoutSessionSchema | schema.GuestActionResponseSchema, 400: ResponseMessage},
        throttle=WriteThrottle(),
    )
    def guest_ticket_checkout(
        self, event_id: UUID, tier_id: UUID, payload: schema.GuestUserDataSchema
    ) -> schema.StripeCheckoutSessionSchema | schema.GuestActionResponseSchema:
        """Purchase a fixed-price ticket without authentication (guest user).

        For online payment: creates guest user and returns Stripe checkout URL immediately (no email
        confirmation). For free/offline/at-the-door tickets: sends confirmation email first. Requires
        event.can_attend_without_login=True. Returns 400 if event doesn't allow guest access, if a
        non-guest account exists with the email, or for PWYC tiers (use /pwyc endpoint instead).
        """
        self.ensure_not_authenticated()
        event = self.get_one(event_id)
        tier = get_object_or_404(
            models.TicketTier.objects.for_user(self.maybe_user()),
            pk=tier_id,
            event=event,
        )
        if tier.price_type == models.TicketTier.PriceType.PWYC:
            raise HttpError(400, str(_("Use /pwyc endpoint for pay-what-you-can tickets")))
        return guest_service.handle_guest_ticket_checkout(
            event, tier, payload.email, payload.first_name, payload.last_name
        )

    @route.post(
        "/{event_id}/tickets/{tier_id}/checkout/pwyc/public",
        url_name="guest_ticket_pwyc_checkout",
        response={200: schema.StripeCheckoutSessionSchema | schema.GuestActionResponseSchema, 400: ResponseMessage},
        throttle=WriteThrottle(),
    )
    def guest_ticket_pwyc_checkout(
        self, event_id: UUID, tier_id: UUID, payload: schema.GuestPWYCCheckoutSchema
    ) -> schema.StripeCheckoutSessionSchema | schema.GuestActionResponseSchema:
        """Purchase a PWYC ticket without authentication (guest user).

        For online payment: creates guest user and returns Stripe checkout URL immediately. For
        free/offline/at-the-door tickets: sends confirmation email first. Validates PWYC amount
        is within tier bounds. Requires event.can_attend_without_login=True. Returns 400 if event
        doesn't allow guest access, if a non-guest account exists, or if PWYC amount is invalid.
        """
        self.ensure_not_authenticated()
        event = self.get_one(event_id)
        tier = get_object_or_404(
            models.TicketTier.objects.for_user(self.maybe_user()),
            pk=tier_id,
            event=event,
        )
        if tier.price_type != models.TicketTier.PriceType.PWYC:
            raise HttpError(400, str(_("This endpoint is only for pay-what-you-can tickets")))
        return guest_service.handle_guest_ticket_checkout(
            event, tier, payload.email, payload.first_name, payload.last_name, pwyc_amount=payload.pwyc
        )
