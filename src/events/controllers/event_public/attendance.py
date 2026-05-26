from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra import (
    api_controller,
    route,
)

from common.authentication import I18nJWTAuth, OptionalAuth
from common.schema import ResponseMessage
from common.throttling import QuestionnaireSubmissionThrottle, WriteThrottle
from events import models, schema
from events.service import (
    bookmark_service,
    event_questionnaire_service,
    event_service,
    feedback_service,
    ticket_service,
)
from events.service.event_manager import (
    EligibilityService,
    EventManager,
    EventUserEligibility,
    NextStep,
    UserIsIneligibleError,
)
from events.service.ticket_service import UserEventStatus
from events.service.waitlist_service import enqueue_waitlist_processing
from questionnaires.models import Questionnaire, QuestionnaireSubmission
from questionnaires.schema import (
    QuestionnaireSchema,
    QuestionnaireSubmissionOrEvaluationSchema,
    QuestionnaireSubmissionResponseSchema,
    QuestionnaireSubmissionSchema,
)
from questionnaires.tasks import evaluate_questionnaire_submission

from .base import EventPublicBaseController


@api_controller("/events", auth=OptionalAuth(), tags=["Events"])
class EventPublicAttendanceController(EventPublicBaseController):
    """Handles RSVP, waitlist, invitations, status, and questionnaire operations."""

    @route.get(
        "/{uuid:event_id}/my-status",
        url_name="get_my_event_status",
        response=schema.EventUserStatusResponse | EventUserEligibility,
        auth=I18nJWTAuth(),
    )
    def get_my_event_status(self, event_id: UUID) -> schema.EventUserStatusResponse | EventUserEligibility:
        """Check the authenticated user's current status and eligibility for an event.

        Returns user's tickets, RSVP status, and purchase limits. For events requiring tickets,
        returns all user's tickets for this event along with remaining purchase capacity.
        For non-ticketed events, returns RSVP status. If user has no status yet, returns
        eligibility check result explaining what steps are needed to attend.

        **Response Fields:**
        - `tickets`: List of user's tickets (PENDING, ACTIVE, or CANCELLED)
        - `rsvp`: User's RSVP status (for non-ticketed events)
        - `can_purchase_more`: Whether user can purchase additional tickets
        - `remaining_tickets`: Per-tier remaining ticket counts. Each item has `tier_id` and
          `remaining` (int or null for unlimited). Empty list for RSVP-only events.
        - `feedback_questionnaires`: Questionnaire IDs available for feedback (only after event ends)

        Use this to determine which action to show users (buy more tickets, view tickets,
        RSVP, fill questionnaire, leave feedback, etc.).
        """
        event = self.get_one(event_id)
        user = self.user()
        status = ticket_service.get_user_event_status(event, user)

        if isinstance(status, UserEventStatus):
            # Get feedback questionnaires if event has ended and user attended
            feedback_questionnaire_ids = feedback_service.get_feedback_questionnaires_for_user(event, user)

            return schema.EventUserStatusResponse(
                tickets=[schema.UserTicketSchema.from_orm(t) for t in status.tickets],
                rsvp=schema.EventRSVPSchema.from_orm(status.rsvp) if status.rsvp else None,
                can_purchase_more=status.can_purchase_more,
                remaining_tickets=[
                    schema.TierRemainingTicketsSchema(tier_id=r.tier_id, remaining=r.remaining, sold_out=r.sold_out)
                    for r in status.remaining_tickets
                ],
                feedback_questionnaires=feedback_questionnaire_ids,
            )

        # EventUserEligibility - return as-is
        return status

    @route.post(
        "/{uuid:event_id}/invitation-requests",
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
        submitted a request for this event. Check GET /{uuid:event_id}/my-status to see if you
        need an invitation.
        """
        event = self.get_one(event_id)
        return 201, event_service.create_invitation_request(event, self.user(), message=payload.message)

    @route.post(
        "/{uuid:event_id}/rsvp/{answer}",
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
        "/{uuid:event_id}/bookmark",
        url_name="bookmark_event",
        response={201: schema.EventBookmarkSchema},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def bookmark_event(self, event_id: UUID) -> tuple[int, models.EventBookmark]:
        """Bookmark an event to find it again later.

        Saving an event is a private "save for later" action: it does not grant access,
        notify anyone, or change your eligibility. You can only bookmark events you can
        currently see (including unlisted events reached via a direct link). Idempotent —
        bookmarking an already-bookmarked event returns the existing bookmark.
        """
        event = self.get_one(event_id)
        return 201, bookmark_service.bookmark_event(self.user(), event)

    @route.delete(
        "/{uuid:event_id}/bookmark",
        url_name="unbookmark_event",
        response={204: None},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def unbookmark_event(self, event_id: UUID) -> tuple[int, None]:
        """Remove your bookmark for an event.

        Idempotent — succeeds with 204 whether or not a bookmark existed. Works even if the
        event is no longer visible to you, so a stale bookmark can always be cleared.
        """
        bookmark_service.unbookmark_event(self.user(), event_id)
        return 204, None

    @route.post(
        "/{uuid:event_id}/waitlist/join",
        url_name="join_waitlist",
        response={200: ResponseMessage, 400: ResponseMessage, 409: ResponseMessage},
        auth=I18nJWTAuth(),
        throttle=WriteThrottle(),
    )
    def join_waitlist(self, event_id: UUID) -> ResponseMessage:
        """Join the waitlist for a full event.

        Allows users to join the event waitlist when the event is at capacity. Users will be
        notified when spots become available. Returns 400 if the event doesn't have an open
        waitlist, 409 if the event has available capacity (the FE should refresh and let the
        user register directly), or a 4xx eligibility error if some other gate blocks the user.
        """
        event = self.get_one(event_id)

        if not event.waitlist_open:
            raise HttpError(400, str(_("This event does not have an open waitlist.")))

        user = self.user()

        # Idempotency: existing waitlist member gets the same 200 they did before.
        if models.EventWaitList.objects.filter(event=event, user=user).exists():
            return ResponseMessage(message=str(_("You are already on the waitlist for this event.")))

        # Run the full eligibility pipeline. We only proceed to join the waitlist
        # when the *only* obstacle is capacity (the user could otherwise register).
        eligibility = EligibilityService(user, event).check_eligibility()
        if eligibility.allowed:
            # Event has capacity right now — the user must have been looking at a
            # stale page. Tell the FE (via 409 Conflict) to refresh and register
            # directly.
            raise HttpError(409, str(_("Event has available capacity. Please refresh and register directly.")))

        waitlist_next_steps = {NextStep.JOIN_WAITLIST, NextStep.WAIT_FOR_OPEN_SPOT}
        if eligibility.next_step not in waitlist_next_steps:
            # Some other gate blocks them (blacklist, invitation required, etc.).
            # They couldn't claim an offer even if they got one.
            raise UserIsIneligibleError(
                message=eligibility.reason or str(_("You are not eligible to join the waitlist.")),
                eligibility=eligibility,
            )

        try:
            with transaction.atomic():
                models.EventWaitList.objects.create(event=event, user=user)
        except IntegrityError:
            # Lost the race against another tab/request. Treat as idempotent
            # success — the user IS on the waitlist now.
            return ResponseMessage(message=str(_("You are already on the waitlist for this event.")))
        # Self-healing: if a seat is currently free (e.g. capacity bumped, or a
        # cancellation landed between page-load and click), the service will see
        # this user at the front of the queue and immediately create an offer.
        enqueue_waitlist_processing(event.id)
        return ResponseMessage(message=str(_("Successfully joined the waitlist.")))

    @route.delete(
        "/{uuid:event_id}/waitlist/leave",
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

        user = self.user()
        had_offer = False
        with transaction.atomic():
            offer = (
                models.WaitlistOffer.objects.select_for_update()
                .filter(
                    event=event,
                    user=user,
                    status=models.WaitlistOffer.WaitlistOfferStatus.PENDING,
                    expires_at__gt=timezone.now(),
                )
                .first()
            )
            if offer is not None:
                offer.status = models.WaitlistOffer.WaitlistOfferStatus.EXPIRED
                offer.save(update_fields=["status"])
                had_offer = True
            models.EventWaitList.objects.filter(event=event, user=user).delete()
        if had_offer:
            # The user gave up their reserved seat — let the next person in.
            enqueue_waitlist_processing(event.id)
        return ResponseMessage(message=str(_("Successfully left the waitlist.")))

    @route.get(
        "/{uuid:event_id}/questionnaire/{questionnaire_id}", url_name="get_questionnaire", response=QuestionnaireSchema
    )
    def get_questionnaire(self, event_id: UUID, questionnaire_id: UUID) -> QuestionnaireSchema:
        """Retrieve a questionnaire for an event.

        For admission questionnaires: Returns the questionnaire structure with all sections
        and questions. Questions may be shuffled based on questionnaire settings.

        For feedback questionnaires: Only accessible after the event has ended and only
        for users who attended the event (RSVP YES or active/checked-in ticket).
        """
        event = self.get_one(event_id)
        org_questionnaire = self.get_org_questionnaire_for_event(event, questionnaire_id)

        # Validate access for FEEDBACK questionnaires (requires authentication + attendance)
        if org_questionnaire.questionnaire_type == models.OrganizationQuestionnaire.QuestionnaireType.FEEDBACK:
            user = self.maybe_user()
            if user.is_anonymous:
                raise HttpError(401, str(_("Authentication required to access feedback questionnaire.")))
            # Don't check if already submitted for viewing - users can view but can't re-submit
            feedback_service.validate_feedback_questionnaire_access(
                user, event, org_questionnaire, check_already_submitted=False
            )

        questionnaire_service = self.get_questionnaire_service(questionnaire_id)
        return questionnaire_service.build()

    @route.post(
        "/{uuid:event_id}/questionnaire/{questionnaire_id}/submit",
        url_name="submit_questionnaire",
        response={200: QuestionnaireSubmissionOrEvaluationSchema, 400: ResponseMessage},
        auth=I18nJWTAuth(),
        throttle=QuestionnaireSubmissionThrottle(),
    )
    def submit_questionnaire(
        self, event_id: UUID, questionnaire_id: UUID, submission: QuestionnaireSubmissionSchema
    ) -> QuestionnaireSubmissionOrEvaluationSchema:
        """Submit answers to an event questionnaire.

        For admission questionnaires: Validates all required questions are answered. If submission
        status is 'ready', triggers automatic evaluation (may use LLM for free-text answers).
        Depending on the questionnaire's evaluation_mode, results may be immediate or pending review.

        For feedback questionnaires: Only accessible after the event has ended and only for users
        who attended the event. Feedback submissions are not evaluated (no approval/rejection).
        """
        event = self.get_one(event_id)
        org_questionnaire = self.get_org_questionnaire_for_event(event, questionnaire_id)
        is_feedback = (
            org_questionnaire.questionnaire_type == models.OrganizationQuestionnaire.QuestionnaireType.FEEDBACK
        )

        # Validate based on questionnaire type
        if is_feedback:
            # Feedback questionnaires: validate event ended + user attended
            feedback_service.validate_feedback_questionnaire_access(self.user(), event, org_questionnaire)
        else:
            # Admission questionnaires: check application deadline (falls back to event start if not set)
            if timezone.now() > event.effective_apply_deadline:
                raise HttpError(400, str(_("The application deadline has passed.")))

        questionnaire_service = self.get_questionnaire_service(questionnaire_id)

        # Submit questionnaire and create tracking record atomically
        db_submission = event_questionnaire_service.submit_event_questionnaire(
            user=self.user(),
            event=event,
            questionnaire_service=questionnaire_service,
            org_questionnaire=org_questionnaire,
            submission_schema=submission,
        )

        # Trigger automatic evaluation only for questionnaires that require it
        if not is_feedback and org_questionnaire.requires_evaluation:
            evaluation_mode = questionnaire_service.questionnaire.evaluation_mode
            if submission.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY and evaluation_mode in (
                Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC,
                Questionnaire.QuestionnaireEvaluationMode.HYBRID,
            ):
                transaction.on_commit(lambda: evaluate_questionnaire_submission.delay(str(db_submission.pk)))

        return QuestionnaireSubmissionResponseSchema.from_orm(db_submission)
