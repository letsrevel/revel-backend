import typing as t
from uuid import UUID

from django.db.models import QuerySet
from django.http import Http404
from django.shortcuts import get_object_or_404
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
from accounts.schema import MinimalRevelUserSchema
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
        """Get an event token if exists."""
        if et := self.context.request.GET.get("et"):  # type: ignore[union-attr]
            return event_service.get_event_token(et)
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
        ],
    )
    def list_events(
        self,
        params: filters.EventFilterSchema = Query(...),  # type: ignore[type-arg]
        order_by: t.Literal["start", "-start", "distance"] = "distance",
        include_past: bool = False,
    ) -> QuerySet[models.Event]:
        """List all organizations."""
        qs = params.filter(self.get_queryset(include_past=include_past or params.past_events is True))
        if order_by == "distance":
            return event_service.order_by_distance(self.user_location(), qs)
        return qs.order_by(order_by)

    @route.post(
        "/claim-invitation/{token}",
        url_name="event_claim_invitation",
        response={200: schema.MinimalEventSchema, 400: ResponseMessage},
        auth=JWTAuth(),
        throttle=WriteThrottle(),
    )
    def claim_invitation(self, token: str) -> tuple[int, models.Event | ResponseMessage]:
        """Request an invitation to an event."""
        if invitation := event_service.claim_invitation(self.user(), token):
            return 200, invitation.event
        return 400, ResponseMessage(message="The token is invalid or expired.")

    @route.get(
        "/{event_id}/attendee-list",
        url_name="event_attendee_list",
        response=PaginatedResponseSchema[MinimalRevelUserSchema],
        auth=JWTAuth(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def get_event_attendees(self, event_id: UUID) -> QuerySet[RevelUser]:
        """Get the attendees for this event."""
        event = self.get_one(event_id)
        return event.attendees(self.user())

    @route.get(
        "/{event_id}/my-status",
        url_name="get_my_event_status",
        response=schema.EventUserStatusSchema | EventUserEligibility,
        auth=JWTAuth(),
    )
    def get_my_event_status(self, event_id: UUID) -> schema.EventUserStatusSchema | EventUserEligibility:
        """Return the user's status for a specific event."""
        event = self.get_one(event_id)
        if (
            ticket := models.Ticket.objects.select_related("tier").filter(event=event, user_id=self.user().id).first()
        ) and event.requires_ticket:
            return schema.EventTicketSchema.from_orm(ticket)
        elif rsvp := models.EventRSVP.objects.filter(event=event, user_id=self.user().id).first():
            return schema.EventRSVPSchema.from_orm(rsvp)
        return EventManager(self.user(), event).check_eligibility()

    @route.post(
        "/{event_id}/request-invitation",
        url_name="request_invitation",
        response={201: schema.EventInvitationRequestSchema, 400: ResponseMessage},
        auth=JWTAuth(),
        throttle=WriteThrottle(),
    )
    def request_invitation(
        self, event_id: UUID, payload: schema.EventInvitationRequestCreateSchema
    ) -> tuple[int, models.EventInvitationRequest | ResponseMessage]:
        """Request an invitation to an event."""
        event = self.get_one(event_id)
        invitation_request, created = models.EventInvitationRequest.objects.get_or_create(
            event=event,
            user=self.user(),
            defaults=payload.model_dump(),
        )
        if not created:
            return 400, ResponseMessage(message="You have already requested an invitation to this event.")
        return 201, invitation_request

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
        """List all visible resources for a specific event."""
        event = self.get_one(event_id)
        qs = models.AdditionalResource.objects.for_user(self.maybe_user()).filter(events=event)
        return params.filter(qs)

    @route.delete(
        "/invitation-request/{request_id}",
        url_name="delete_invitation_request",
        response={204: None},
        auth=JWTAuth(),
        throttle=WriteThrottle(),
    )
    def delete_invitation_request(self, request_id: UUID) -> tuple[int, None]:
        """Delete an invitation request."""
        invitation_request = get_object_or_404(models.EventInvitationRequest, pk=request_id, user_id=self.user().id)
        invitation_request.delete()
        return 204, None

    @route.get(
        "/me/pending_invitation_requests",
        url_name="list_user_invitation_requests",
        response=PaginatedResponseSchema[schema.EventInvitationRequestSchema],
        auth=JWTAuth(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["event__name", "event__description", "message"])
    def list_user_invitation_requests(
        self,
        event_id: UUID | None = None,
        status: models.EventInvitationRequest.Status = models.EventInvitationRequest.Status.PENDING,
    ) -> QuerySet[models.EventInvitationRequest]:
        """List all pending invitation requests for the current user."""
        qs = models.EventInvitationRequest.objects.select_related("event").filter(user=self.user(), status=status)
        if event_id:
            qs = qs.filter(event_id=event_id)
        return qs

    @route.get("/{org_slug}/{event_slug}", url_name="get_event_by_slug", response=schema.EventDetailSchema)
    def get_event_by_slugs(self, org_slug: str, event_slug: str) -> models.Event:
        """Get event by ID."""
        return t.cast(
            models.Event,
            self.get_object_or_exception(self.get_queryset(), slug=event_slug, organization__slug=org_slug),
        )

    @route.get("/{event_id}", url_name="get_event", response=schema.EventDetailSchema)
    def get_event(self, event_id: UUID) -> models.Event:
        """Get event by ID."""
        return self.get_one(event_id)

    @route.post(
        "/{event_id}/rsvp/{answer}",
        url_name="rsvp_event",
        response={200: schema.EventRSVPSchema, 400: EventUserEligibility},
        auth=JWTAuth(),
        throttle=WriteThrottle(),
    )
    def rsvp_event(self, event_id: UUID, answer: models.EventRSVP.Status) -> models.EventRSVP:
        """RSVP event by ID."""
        event = self.get_one(event_id)
        manager = EventManager(self.user(), event)
        return manager.rsvp(answer)

    @route.get(
        "/{event_id}/tickets/tiers",
        url_name="tier_list",
        response={200: list[schema.TierSchema]},
    )
    def list_tiers(self, event_id: UUID) -> models.event.TicketTierQuerySet:
        """List all available tickets for a specific event."""
        event = self.get_one(event_id)
        return models.TicketTier.objects.for_user(self.user()).filter(event=event)

    @route.post(
        "/{event_id}/tickets/{tier_id}/checkout",
        url_name="ticket_checkout",
        response={200: schema.StripeCheckoutSessionSchema | schema.EventTicketSchema},
        auth=JWTAuth(),
        throttle=WriteThrottle(),
        permissions=[CanPurchaseTicket()],
    )
    def ticket_checkout(
        self,
        event_id: UUID,
        tier_id: UUID,
    ) -> schema.StripeCheckoutSessionSchema | schema.EventTicketSchema:
        """Create a Stripe checkout session for purchasing a ticket."""
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
        response={200: schema.StripeCheckoutSessionSchema | schema.EventTicketSchema},
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
        """Create a Stripe checkout session for purchasing a pay-what-you-can ticket."""
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
        """Get questionnaire and build it."""
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
        """Submit questionnaire."""
        self.get_one(event_id)
        questionnaire_service = self.get_questionnaire_service(questionnaire_id)
        db_submission = questionnaire_service.submit(self.user(), submission)
        if submission.status == QuestionnaireSubmission.Status.READY:
            evaluate_questionnaire_submission.delay(str(db_submission.pk))
        return QuestionnaireSubmissionResponseSchema.from_orm(db_submission)
