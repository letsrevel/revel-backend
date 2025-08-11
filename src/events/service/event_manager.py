import abc
import datetime
import uuid
from collections import defaultdict
from decimal import Decimal
from enum import StrEnum

from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone
from django.utils.translation import gettext as _  # we can't use lazy, otherwise pydantic complains
from pydantic import BaseModel

from accounts.models import RevelUser
from events import models
from events.models import EventRSVP, OrganizationQuestionnaire, Ticket, TicketTier
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

from .ticket_service import TicketService


class NextStep(StrEnum):
    REQUEST_INVITATION = "request_invitation"
    BECOME_MEMBER = "become_member"
    COMPLETE_QUESTIONNAIRE = "complete_questionnaire"
    WAIT_FOR_QUESTIONNAIRE_EVALUATION = "wait_for_questionnaire_evaluation"
    WAIT_TO_RETAKE_QUESTIONNAIRE = "wait_to_retake_questionnaire"
    WAIT_FOR_EVENT_TO_OPEN = "wait_for_event_to_open"
    JOIN_WAITLIST = "join_waitlist"
    PURCHASE_TICKET = "purchase_ticket"
    RSVP = "rsvp"


class Reasons(StrEnum):
    MEMBERS_ONLY = "Only members are allowed."
    EVENT_IS_FULL = "Event is full."
    SOLD_OUT = "Sold out"
    QUESTIONNAIRE_MISSING = "Questionnaire has not been filled."
    QUESTIONNAIRE_FAILED = "Questionnaire evaluation was insufficient."
    QUESTIONNAIRE_PENDING_REVIEW = "Waiting for questionnaire evaluation."
    QUESTIONNAIRE_RETAKE_COOLDOWN = "Questionnaire evaluation was insufficient. You can try again in {retry_on}."
    REQUIRES_TICKET = "Requires a ticket."
    MUST_RSVP = "Must RSVP"
    REQUIRES_INVITATION = "Requires invitation."
    REQUIRES_PURCHASE = "Requires purchase."
    NOTHING_TO_PURCHASE = "Nothing to purchase."
    EVENT_IS_NOT_OPEN = "Event is not open."
    EVENT_HAS_FINISHED = "Event has finished."
    RSVP_DEADLINE_PASSED = "The RSVP deadline has passed."
    NO_TICKETS_ON_SALE = "Tickets are not currently on sale."


class EventUserEligibility(BaseModel):
    allowed: bool
    event_id: uuid.UUID
    reason: str | None = None  # we don't use the enum here because we want translation
    next_step: NextStep | None = None
    questionnaires_missing: list[uuid.UUID] | None = None
    questionnaires_pending_review: list[uuid.UUID] | None = None
    questionnaires_failed: list[uuid.UUID] | None = None
    retry_on: datetime.datetime | None = None


class UserIsIneligibleError(Exception):
    def __init__(self, message: str, eligibility: EventUserEligibility) -> None:
        """Custom Exception class for ticket eligibility."""
        super().__init__(message)
        self.eligibility = eligibility


class BaseEligibilityGate(abc.ABC):
    """Abstract Base Class for a composable eligibility check."""

    def __init__(self, handler: "EligibilityService"):
        """Initialize the eligibility check."""
        self.handler = handler
        self.user = handler.user
        self.event = handler.event

    @abc.abstractmethod
    def check(self) -> EventUserEligibility | None:
        """Abstract method for checking eligibility."""


class PrivilegedAccessGate(BaseEligibilityGate):
    """Gate #1: Allows access for organization owners and staff members immediately."""

    def check(self) -> EventUserEligibility | None:
        """Check whether a user is staff."""
        if self.event.organization.owner_id == self.user.id or self.user.id in self.handler.staff_ids:
            return EventUserEligibility(allowed=True, tier="staff", event_id=self.event.pk)
        return None


class EventStatusGate(BaseEligibilityGate):
    """Gate #2: Checks if the event is open for participation."""

    def check(self) -> EventUserEligibility | None:
        """Check that the event is open for participation."""
        if self.event.end < timezone.now():
            return EventUserEligibility(
                allowed=False,
                event_id=self.event.id,
                reason=_(Reasons.EVENT_HAS_FINISHED),
            )
        if self.event.status != models.Event.Status.OPEN:
            return EventUserEligibility(
                allowed=False,
                event_id=self.event.id,
                reason=_(Reasons.EVENT_IS_NOT_OPEN),
                next_step=NextStep.WAIT_FOR_EVENT_TO_OPEN,
            )
        return None


class InvitationGate(BaseEligibilityGate):
    """Gate #3: For private events, ensures the user has a valid invitation."""

    def check(self) -> EventUserEligibility | None:
        """Check if invitation is valid."""
        if self.event.event_type == models.Event.Types.PRIVATE and not self.handler.invitation:
            return EventUserEligibility(
                allowed=False,
                event_id=self.event.id,
                reason=_(Reasons.REQUIRES_INVITATION),
                next_step=NextStep.REQUEST_INVITATION,
            )
        return None


class MembershipGate(BaseEligibilityGate):
    """Gate #4: For members-only events, ensures the user is a member."""

    def check(self) -> EventUserEligibility | None:
        """Check if membership is in order."""
        if self.handler.waives_membership_required():
            return None
        if self.event.event_type == models.Event.Types.MEMBERS_ONLY and self.user.id not in self.handler.member_ids:
            return EventUserEligibility(
                allowed=False, event_id=self.event.id, reason=_(Reasons.MEMBERS_ONLY), next_step=NextStep.BECOME_MEMBER
            )
        return None


class QuestionnaireGate(BaseEligibilityGate):
    """Gate #5: Check questionnaire."""

    def check(self) -> EventUserEligibility | None:
        """Check if the questionnaires are in order."""
        if missing := self._check_missing_questionnaires():
            return missing
        if pending_review := self._check_pending_review():
            return pending_review
        if failed := self._check_failed():
            return failed
        return None

    def _check_missing_questionnaires(self) -> EventUserEligibility | None:
        questionnaires_missing = []
        for org_questionnaire in self.handler.event.organization.relevant_org_questionnaires:  # type: ignore[attr-defined]
            # Look up the submission in our O(1) map. No database query.
            submissions = self.handler.submission_map.get(org_questionnaire.questionnaire_id)
            if submissions is None or submissions[0].status != QuestionnaireSubmission.Status.READY:
                questionnaires_missing.append(org_questionnaire.questionnaire_id)

        if questionnaires_missing:
            return EventUserEligibility(
                allowed=False,
                event_id=self.event.id,
                reason=_(Reasons.QUESTIONNAIRE_MISSING),
                next_step=NextStep.COMPLETE_QUESTIONNAIRE,
                questionnaires_missing=questionnaires_missing,
            )
        return None

    def _check_pending_review(self) -> EventUserEligibility | None:
        questionnaires_pending_review = []
        for org_questionnaire in self.handler.event.organization.relevant_org_questionnaires:  # type: ignore[attr-defined]
            # Look up the submission in our O(1) map. No database query.
            if submissions := self.handler.submission_map.get(org_questionnaire.questionnaire_id):
                evaluation = getattr(submissions[0], "evaluation", None)
                if evaluation is None or evaluation.status == QuestionnaireEvaluation.Status.PENDING_REVIEW:
                    questionnaires_pending_review.append(org_questionnaire.questionnaire_id)
        if questionnaires_pending_review:
            return EventUserEligibility(
                allowed=False,
                reason=_(Reasons.QUESTIONNAIRE_PENDING_REVIEW),
                event_id=self.event.id,
                next_step=NextStep.WAIT_FOR_QUESTIONNAIRE_EVALUATION,
                questionnaires_pending_review=questionnaires_pending_review,
            )
        return None

    def _check_failed(self) -> EventUserEligibility | None:
        failed_questionnaires = []
        questionnaires_missing = []
        for org_questionnaire in self.handler.event.organization.relevant_org_questionnaires:  # type: ignore[attr-defined]
            # Look up the submission in our O(1) map. No database query.
            questionnaire = org_questionnaire.questionnaire
            submissions = self.handler.submission_map.get(questionnaire.id)
            if not submissions:
                continue
            submission = submissions[0]
            evaluation = getattr(submission, "evaluation", None)
            if evaluation is None:
                continue
            if evaluation.status != QuestionnaireEvaluation.Status.REJECTED:
                continue
            if 0 < questionnaire.max_attempts <= len(submissions):
                failed_questionnaires.append(org_questionnaire.questionnaire_id)
                continue
            if questionnaire.can_retake_after is None:
                questionnaires_missing.append(questionnaire.id)
            elif submission.submitted_at + questionnaire.can_retake_after < timezone.now():
                questionnaires_missing.append(questionnaire.id)
            else:
                retry_on = submission.submitted_at + questionnaire.can_retake_after
                return EventUserEligibility(
                    allowed=False,
                    reason=_(Reasons.QUESTIONNAIRE_FAILED),
                    event_id=self.event.id,
                    next_step=NextStep.WAIT_TO_RETAKE_QUESTIONNAIRE,
                    retry_on=retry_on,
                )
        if failed_questionnaires:
            return EventUserEligibility(
                allowed=False,
                reason=_(Reasons.QUESTIONNAIRE_FAILED),
                event_id=self.event.id,
                questionnaires_failed=failed_questionnaires,
            )
        if questionnaires_missing:
            return EventUserEligibility(
                allowed=False,
                event_id=self.event.id,
                reason=_(Reasons.QUESTIONNAIRE_MISSING),
                next_step=NextStep.COMPLETE_QUESTIONNAIRE,
                questionnaires_missing=questionnaires_missing,
            )
        return None


class RSVPDeadlineGate(BaseEligibilityGate):
    """Gate #6: Checks RSVP deadline for events that do not require tickets."""

    def check(self) -> EventUserEligibility | None:
        """Check if RSVP deadline has passed for non-ticket events."""
        # Only check for events that don't require tickets
        if self.event.requires_ticket:
            return None

        # No deadline set
        if not self.event.rsvp_before:
            return None

        # Check if user has invitation that waives RSVP deadline
        if self.handler.invitation and self.handler.invitation.waives_rsvp_deadline:
            return None

        # Check if deadline has passed
        current_time = timezone.now()
        if current_time > self.event.rsvp_before:
            return EventUserEligibility(
                allowed=False,
                event_id=self.event.id,
                reason=_(Reasons.RSVP_DEADLINE_PASSED),
                next_step=None,
            )
        return None


class AvailabilityGate(BaseEligibilityGate):
    """Gate #7: Checks if the event has space available for another attendee."""

    def check(self) -> EventUserEligibility | None:
        """Check if the event has space available for another attendee."""
        if self.event.max_attendees == 0 or self.handler.overrides_max_attendees():
            return None

        if self._get_attendee_count() >= self.event.max_attendees:
            return EventUserEligibility(
                allowed=False,
                event_id=self.event.id,
                reason=_(Reasons.EVENT_IS_FULL),
                next_step=NextStep.JOIN_WAITLIST if self.event.waitlist_open else None,
            )
        return None

    def _get_attendee_count(self) -> int:
        if self.event.requires_ticket:
            return len({ticket.user_id for ticket in self.event.tickets.all()})
        return self.event.rsvps.count()


class TicketSalesGate(BaseEligibilityGate):
    """Gate #8: Checks if tickets are currently on sale for ticket-required events."""

    def check(self) -> EventUserEligibility | None:
        """Check if there's at least one ticket tier with active sales."""
        # Only check for events that require tickets
        if not self.event.requires_ticket:
            return None

        # Check if there are any ticket tiers with active sales
        current_time = timezone.now()
        for tier in self.event.ticket_tiers.all():
            # If no sales window is set, assume tickets are always on sale
            if tier.sales_start_at is None and tier.sales_end_at is None:
                return None

            # Check if current time is within sales window
            sales_active = True
            if tier.sales_start_at and current_time < tier.sales_start_at:
                sales_active = False

            # Use event end date if sales_end_at is not provided
            sales_end_time = tier.sales_end_at or self.event.start
            if sales_end_time and current_time > sales_end_time:
                sales_active = False

            if sales_active:
                return None  # At least one tier has active sales

        # No tiers have active sales
        return EventUserEligibility(
            allowed=False,
            event_id=self.event.id,
            reason=_(Reasons.NO_TICKETS_ON_SALE),
            next_step=None,
        )


class EligibilityService:
    """The Eligibility Service Class.

    This class is responsible for checking if a user is eligible to participate in an event.
    Most notably, it performs eligibility checks, raises explicit errors with relevant information.
    """

    ELIGIBILITY_GATES = [
        PrivilegedAccessGate,
        EventStatusGate,
        RSVPDeadlineGate,
        InvitationGate,
        MembershipGate,
        QuestionnaireGate,
        AvailabilityGate,
        TicketSalesGate,
    ]

    def __init__(self, user: RevelUser, event: models.Event) -> None:
        """Initializes the handler, pre-fetching all required data in a highly optimized way.

        This ensures all subsequent checks are performed in-memory without further database hits.
        """
        # First, get the user with all their relevant submissions and evaluations.
        # This is a separate query but is necessary and efficient for the questionnaire check.
        self.user = RevelUser.objects.prefetch_related(
            Prefetch(
                "questionnaire_submissions",
                queryset=QuestionnaireSubmission.objects.ready().select_related("evaluation"),
            )
        ).get(pk=user.pk)

        event_link_filter = Q(events=event)
        if event.event_series:
            event_link_filter |= Q(event_series=event.event_series)

        questionnaire_filter = event_link_filter & Q(
            questionnaire_type=OrganizationQuestionnaire.Types.ADMISSION,
            questionnaire__status=Questionnaire.Status.PUBLISHED,
        )

        # Now, fetch the event and all its related data in a single, optimized query.
        self.event = (
            models.Event.objects.select_related("organization")
            .prefetch_related(
                Prefetch("tickets", queryset=models.Ticket.objects.only("user_id")),
                Prefetch(
                    "invitations",
                    queryset=models.EventInvitation.objects.filter(user=user).select_related("tier"),
                ),
                # Use .only("id") to fetch lightweight model instances with only the ID populated.
                Prefetch(
                    "organization__staff_members",
                    queryset=RevelUser.objects.only("id"),
                    to_attr="staff_members_prefetched",
                ),
                Prefetch("organization__members", queryset=RevelUser.objects.only("id"), to_attr="members_prefetched"),
                Prefetch(
                    "organization__org_questionnaires",
                    queryset=models.OrganizationQuestionnaire.objects.filter(questionnaire_filter).distinct(),
                    to_attr="relevant_org_questionnaires",
                ),
                Prefetch(
                    "rsvps",
                    queryset=models.EventRSVP.objects.filter(status=EventRSVP.Status.YES),
                ),
                "ticket_tiers",  # Prefetch ticket tiers for sales window checking
            )
            .get(pk=event.pk)
        )

        # Create sets of IDs from the prefetched lightweight model instances.
        self.staff_ids = {staff.id for staff in self.event.organization.staff_members_prefetched}  # type: ignore[attr-defined]
        self.member_ids = {member.id for member in self.event.organization.members_prefetched}  # type: ignore[attr-defined]

        self.invitation = self.event.invitations.first()
        self.submission_map: dict[uuid.UUID, list[QuestionnaireSubmission]] = defaultdict(list)
        for sub in self.user.questionnaire_submissions.all():
            self.submission_map[sub.questionnaire_id].append(sub)

        self._gates = [gate(self) for gate in self.ELIGIBILITY_GATES]  # type: ignore[abstract]

    def check_eligibility(self, bypass: bool = False) -> EventUserEligibility:
        """Checks eligibility using the fully prefetched, in-memory data.

        This method SHOULD make ZERO database queries.

        Returns:
            TicketEligibility
        """
        if bypass:
            return EventUserEligibility(allowed=True, event_id=self.event.pk)

        for gate in self._gates:
            if result := gate.check():
                return result

        return EventUserEligibility(allowed=True, event_id=self.event.id)

    def waives_questionnaire(self) -> bool:
        """Overrides questionnaire."""
        return getattr(self.invitation, "waives_questionnaire", False)

    def overrides_max_attendees(self) -> bool:
        """Overrides max attendees."""
        return getattr(self.invitation, "overrides_max_attendees", False)

    def waives_membership_required(self) -> bool:
        """Overrides membership required."""
        return getattr(self.invitation, "waives_membership_required", False)

    def waives_purchase(self) -> bool:
        """Overrides purchase requirement - grants complimentary access."""
        return getattr(self.invitation, "waives_purchase", False)


class EventManager:
    def __init__(self, user: RevelUser, event: models.Event) -> None:
        """The Event Manager Class.

        It is responsible to handle rsvp and ticket issuance for events,
        ensuring eligibility checks pass and there are no race conditions.
        """
        self.user = user
        self.event = event
        self.eligibility_service = EligibilityService(user, event)

    @transaction.atomic
    def rsvp(self, answer: EventRSVP.Status, bypass_eligibility_checks: bool = False) -> EventRSVP:
        """Rsvp to an event.

        A user can RSVP if an Event DOES not require a ticket, AND:
        - an event is private, and the user has an invitation for that event
        - an event is members only and the user is a member (or staff member)
        - an event is public

        Returns:
            EventRSVP

        Raises:
            UserIsIneligibleError
        """
        if self.event.requires_ticket:
            raise UserIsIneligibleError(
                message="You must get a ticket for this event.",
                eligibility=EventUserEligibility(
                    allowed=False,
                    event_id=self.event.id,
                    next_step=NextStep.PURCHASE_TICKET,
                    reason=_(Reasons.REQUIRES_TICKET),
                ),
            )
        eligibility = self.check_eligibility(bypass=bypass_eligibility_checks)
        if not eligibility.allowed:
            raise UserIsIneligibleError("The user is not eligible for this event.", eligibility=eligibility)

        self._assert_capacity(use_tickets=False, tier=None)

        rsvp, _created = EventRSVP.objects.update_or_create(
            user=self.user,
            event=self.event,
            defaults={"status": answer},
        )
        return rsvp

    @transaction.atomic
    def create_ticket(
        self, tier: TicketTier, bypass_eligibility_checks: bool = False, price_override: Decimal | None = None
    ) -> Ticket | str:
        """Create a ticket for the user and event.

        Returns:
            Ticket: A ticket for the user and event.

        Raises:
            UserIsIneligibleError
        """
        if not self.event.requires_ticket:
            raise UserIsIneligibleError(
                message="You don't need a ticket for this event.",
                eligibility=EventUserEligibility(
                    allowed=False, event_id=self.event.id, next_step=NextStep.RSVP, reason=_(Reasons.MUST_RSVP)
                ),
            )
        TicketTier.objects.select_for_update().get(pk=tier.pk)
        eligibility = self.check_eligibility(bypass=bypass_eligibility_checks)
        if not eligibility.allowed:
            raise UserIsIneligibleError("The user is not eligible for this event.", eligibility=eligibility)

        self._assert_capacity(use_tickets=True, tier=tier)

        # Check if user has invitation that waives purchase
        if self.eligibility_service.waives_purchase():
            return self._create_complimentary_ticket(tier)

        ticket_service = TicketService(event=self.event, user=self.user, tier=tier)
        return ticket_service.checkout(price_override=price_override)

    def check_eligibility(self, bypass: bool = False) -> EventUserEligibility:
        """Call the eligibility check."""
        return self.eligibility_service.check_eligibility(bypass=bypass)

    def _create_complimentary_ticket(self, tier: TicketTier) -> Ticket:
        """Create a complimentary (free) ACTIVE ticket, bypassing payment flow.

        This method is called when a user has an invitation with waives_purchase=True.
        """
        from django.db.models import F

        # Increment quantity_sold to respect capacity limits
        TicketTier.objects.select_for_update().filter(pk=tier.pk).update(quantity_sold=F("quantity_sold") + 1)

        # Create an ACTIVE ticket directly, bypassing the payment flow
        ticket = Ticket.objects.create(event=self.event, tier=tier, user=self.user, status=Ticket.Status.ACTIVE)

        return ticket

    def _assert_capacity(self, use_tickets: bool, tier: TicketTier | None) -> None:
        """Raises if the event has no more available attendee slots."""
        if self.event.max_attendees == 0 or self.eligibility_service.overrides_max_attendees():
            return

        if use_tickets:
            count = Ticket.objects.select_for_update().filter(event=self.event).values("user_id").distinct().count()
            if not tier:
                raise ValueError("Tier must be provided for ticket counts.")
            if tier.total_quantity and tier.quantity_sold >= tier.total_quantity:
                raise UserIsIneligibleError(
                    message="Tier is sold out.",
                    eligibility=EventUserEligibility(
                        allowed=False,
                        event_id=self.event.id,
                        next_step=NextStep.JOIN_WAITLIST if self.event.waitlist_open else None,
                        reason=_(Reasons.SOLD_OUT),
                    ),
                )
        else:
            count = EventRSVP.objects.select_for_update().filter(event=self.event, status=EventRSVP.Status.YES).count()

        if count >= self.event.max_attendees:
            raise UserIsIneligibleError(
                message="Event is full.",
                eligibility=EventUserEligibility(
                    allowed=False,
                    event_id=self.event.id,
                    next_step=NextStep.JOIN_WAITLIST if self.event.waitlist_open else None,
                    reason=_(Reasons.EVENT_IS_FULL),
                ),
            )
