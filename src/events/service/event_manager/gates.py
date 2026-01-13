"""Eligibility gate classes for the event eligibility system.

Each gate performs a specific eligibility check. Gates are composed together
by the EligibilityService to determine if a user can participate in an event.
"""

from __future__ import annotations

import abc
import uuid
from typing import TYPE_CHECKING

from django.utils import timezone
from django.utils.translation import gettext as _

from events import models
from events.models import (
    EventInvitationRequest,
    EventRSVP,
    OrganizationMember,
    OrganizationQuestionnaire,
    Ticket,
    WhitelistRequest,
)
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

from .enums import NextStep, Reasons
from .types import EventUserEligibility

if TYPE_CHECKING:
    from accounts.models import RevelUser

    from .service import EligibilityService


class BaseEligibilityGate(abc.ABC):
    """Abstract Base Class for a composable eligibility check."""

    def __init__(self, handler: EligibilityService) -> None:
        """Initialize the eligibility check."""
        self.handler = handler
        self.user: RevelUser = handler.user
        self.event: models.Event = handler.event

    @abc.abstractmethod
    def check(self) -> EventUserEligibility | None:
        """Perform the eligibility check.

        Returns:
            EventUserEligibility if this gate blocks access, None to continue to next gate.
        """


class PrivilegedAccessGate(BaseEligibilityGate):
    """Gate #1: Allows access for organization owners and staff members immediately."""

    def check(self) -> EventUserEligibility | None:
        """Check whether a user is staff."""
        if self.event.organization.owner_id == self.user.id or self.user.id in self.handler.staff_ids:
            return EventUserEligibility(allowed=True, tier="staff", event_id=self.event.pk)
        return None


class BlacklistGate(BaseEligibilityGate):
    """Gate #2: Checks if user is blacklisted or fuzzy-matches a blacklist entry.

    This gate checks two levels of blocking:
    1. Hard block - user is definitively blacklisted (FK match or hard identifier match)
    2. Soft block - user's name fuzzy-matches a blacklist entry (requires verification)
    """

    def check(self) -> EventUserEligibility | None:
        """Check blacklist status."""
        # 1. Hard match - complete block, no recourse
        if self.handler.is_hard_blacklisted:
            return EventUserEligibility(
                allowed=False,
                event_id=self.event.id,
                reason=_(Reasons.BLACKLISTED),
                next_step=None,
            )

        # 2. Active members bypass fuzzy matching (trusted users don't need verification)
        if self.handler.membership_status_map.get(self.user.id) == OrganizationMember.MembershipStatus.ACTIVE:
            return None

        # 3. No fuzzy matches - pass through
        if not self.handler.fuzzy_matched_blacklist_entries:
            return None

        # 4. Already whitelisted - pass through
        if self.handler.is_whitelisted:
            return None

        # 5. Check whitelist request status
        whitelist_request = self.handler.whitelist_request
        if whitelist_request:
            if whitelist_request.status == WhitelistRequest.Status.PENDING:
                return EventUserEligibility(
                    allowed=False,
                    event_id=self.event.id,
                    reason=_(Reasons.WHITELIST_PENDING),
                    next_step=NextStep.WAIT_FOR_WHITELIST_APPROVAL,
                )
            if whitelist_request.status == WhitelistRequest.Status.REJECTED:
                return EventUserEligibility(
                    allowed=False,
                    event_id=self.event.id,
                    reason=_(Reasons.WHITELIST_REJECTED),
                    next_step=None,
                )

        # 6. No request yet - prompt user to request whitelist
        return EventUserEligibility(
            allowed=False,
            event_id=self.event.id,
            reason=_(Reasons.VERIFICATION_REQUIRED),
            next_step=NextStep.REQUEST_WHITELIST,
        )


class EventStatusGate(BaseEligibilityGate):
    """Gate #3: Checks if the event is open for participation."""

    def check(self) -> EventUserEligibility | None:
        """Check that the event is open for participation."""
        if self.event.end < timezone.now():
            return EventUserEligibility(
                allowed=False,
                event_id=self.event.id,
                reason=_(Reasons.EVENT_HAS_FINISHED),
            )
        if self.event.status != models.Event.EventStatus.OPEN:
            return EventUserEligibility(
                allowed=False,
                event_id=self.event.id,
                reason=_(Reasons.EVENT_IS_NOT_OPEN),
                next_step=NextStep.WAIT_FOR_EVENT_TO_OPEN,
            )
        return None


class RSVPDeadlineGate(BaseEligibilityGate):
    """Gate #4: Checks RSVP deadline for events that do not require tickets."""

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


class ApplyDeadlineGate(BaseEligibilityGate):
    """Gate #5: Checks if application deadline has passed for users who still need to apply.

    This gate blocks users who haven't yet submitted an invitation request or completed
    a required questionnaire when the apply_before deadline has passed.
    """

    def check(self) -> EventUserEligibility | None:
        """Check if application deadline has passed and user still needs to apply."""
        # Deadline hasn't passed yet (falls back to event start if apply_before is not set)
        if timezone.now() <= self.event.effective_apply_deadline:
            return None

        # Check if user has invitation that waives application deadline
        if self.handler.invitation and self.handler.invitation.waives_apply_deadline:
            return None

        # Check if user still needs to apply
        if not self._user_needs_to_apply():
            return None

        # Deadline passed and user needs to apply
        return EventUserEligibility(
            allowed=False,
            event_id=self.event.id,
            reason=_(Reasons.APPLICATION_DEADLINE_PASSED),
            next_step=None,
        )

    def _user_needs_to_apply(self) -> bool:
        """Check if user still needs to submit invitation request or questionnaire.

        Returns True if user needs to apply (hasn't yet completed required steps).
        """
        # Check if user needs to submit an invitation request
        if self._needs_invitation_request():
            return True

        # Check if user needs to complete a questionnaire
        if self._needs_questionnaire():
            return True

        return False

    def _needs_invitation_request(self) -> bool:
        """Check if user needs to submit an invitation request."""
        # Only relevant for private events that accept invitation requests
        if self.event.event_type != models.Event.EventType.PRIVATE:
            return False

        if not self.event.accept_invitation_requests:
            return False

        # User already has an invitation
        if self.handler.invitation:
            return False

        # User already submitted a request (pending or rejected)
        if self.handler.invitation_request:
            return False

        return True

    def _needs_questionnaire(self) -> bool:
        """Check if user needs to complete an admission questionnaire."""
        # Get relevant questionnaires for this event
        relevant_questionnaires: list[OrganizationQuestionnaire] = getattr(
            self.event.organization, "relevant_org_questionnaires", []
        )

        if not relevant_questionnaires:
            return False

        # Check if invitation waives questionnaire
        if getattr(self.handler.invitation, "waives_questionnaire", False):
            return False

        # Get user's submissions
        user_submissions = {sub.questionnaire_id: sub for sub in self.user.questionnaire_submissions.all()}

        for org_questionnaire in relevant_questionnaires:
            # Skip if user is member-exempt
            if org_questionnaire.members_exempt and self.user.id in self.handler.member_ids:
                continue

            questionnaire_id = org_questionnaire.questionnaire_id
            submission = user_submissions.get(questionnaire_id)

            # No submission - needs to complete questionnaire
            if not submission:
                return True

            # Check if submission is approved
            evaluation = getattr(submission, "evaluation", None)
            if not evaluation:
                # Pending review - already submitted, doesn't need to apply
                continue

            if evaluation.status != QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED:
                # Failed - may need to retake, which counts as needing to apply
                return True

        return False


class InvitationGate(BaseEligibilityGate):
    """Gate #6: For private events, ensures the user has a valid invitation."""

    def check(self) -> EventUserEligibility | None:
        """Check if invitation is valid."""
        if self.event.event_type != models.Event.EventType.PRIVATE:
            return None

        if self.handler.invitation:
            return None

        # User needs an invitation but doesn't have one - check for existing request
        invitation_request = self.handler.invitation_request
        if invitation_request:
            if invitation_request.status == EventInvitationRequest.InvitationRequestStatus.PENDING:
                return EventUserEligibility(
                    allowed=False,
                    event_id=self.event.id,
                    reason=_(Reasons.INVITATION_REQUEST_PENDING),
                    next_step=NextStep.WAIT_FOR_INVITATION_APPROVAL,
                )
            if invitation_request.status == EventInvitationRequest.InvitationRequestStatus.REJECTED:
                return EventUserEligibility(
                    allowed=False,
                    event_id=self.event.id,
                    reason=_(Reasons.INVITATION_REQUEST_REJECTED),
                    next_step=None,  # No action available after rejection
                )

        # No invitation and no request - allow requesting if enabled
        return EventUserEligibility(
            allowed=False,
            event_id=self.event.id,
            reason=_(Reasons.REQUIRES_INVITATION),
            next_step=NextStep.REQUEST_INVITATION if self.event.accept_invitation_requests else None,
        )


class MembershipGate(BaseEligibilityGate):
    """Gate #7: For members-only events, ensures the user is an active member."""

    def check(self) -> EventUserEligibility | None:
        """Check if membership is in order."""
        if self.handler.waives_membership_required():
            return None

        if self.event.event_type != models.Event.EventType.MEMBERS_ONLY:
            return None

        # Check if user is an active member
        if self.user.id in self.handler.member_ids:
            return None

        # Check if user has a membership but it's not active
        membership_status = self.handler.membership_status_map.get(self.user.id)
        if membership_status is not None and membership_status != OrganizationMember.MembershipStatus.ACTIVE:
            return EventUserEligibility(
                allowed=False,
                event_id=self.event.id,
                reason=_(Reasons.MEMBERSHIP_INACTIVE),
                next_step=None,  # They need to contact the organization to reactivate
            )

        # User has no membership at all
        return EventUserEligibility(
            allowed=False,
            event_id=self.event.id,
            reason=_(Reasons.MEMBERS_ONLY),
            next_step=NextStep.BECOME_MEMBER if self.event.organization.accept_membership_requests else None,
        )


class QuestionnaireGate(BaseEligibilityGate):
    """Gate #8: Check questionnaire requirements."""

    def check(self) -> EventUserEligibility | None:
        """Check if the questionnaires are in order."""
        # Invitation can waive all questionnaire requirements
        if getattr(self.handler.invitation, "waives_questionnaire", False):
            return None
        if missing := self._check_missing_questionnaires():
            return missing
        if pending_review := self._check_pending_review():
            return pending_review
        if failed := self._check_failed():
            return failed
        return None

    def _get_applicable_questionnaires(self) -> list[OrganizationQuestionnaire]:
        """Get questionnaires that apply to this user (excluding waived/exempt ones)."""
        return [
            oq
            for oq in self.handler.event.organization.relevant_org_questionnaires  # type: ignore[attr-defined]
            if not (oq.members_exempt and self.user.id in self.handler.member_ids)
        ]

    def _is_submission_expired(
        self,
        org_questionnaire: OrganizationQuestionnaire,
        submission: QuestionnaireSubmission,
    ) -> bool:
        """Check if an approved submission has expired based on max_submission_age.

        An expired submission means the user must retake the questionnaire.
        Only APPROVED submissions can expire - other states are handled by separate checks.

        Returns:
            True if submission has an APPROVED evaluation that has expired.
            False if no expiration configured, or submission is not approved yet.
        """
        if org_questionnaire.max_submission_age is None:
            return False  # No expiration configured

        evaluation: QuestionnaireEvaluation | None = getattr(submission, "evaluation", None)
        if evaluation is None:
            return False  # No evaluation yet - let _check_pending_review handle this

        if evaluation.status != QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED:
            return False  # Not approved - let _check_failed handle this

        # Check if the approved evaluation has expired
        expiry_time = evaluation.updated_at + org_questionnaire.max_submission_age
        return bool(expiry_time < timezone.now())

    def _check_retake_eligibility(
        self,
        questionnaire: Questionnaire,
        submission: QuestionnaireSubmission,
        questionnaires_missing: list[uuid.UUID],
    ) -> EventUserEligibility | None:
        """Check if user can retake a rejected questionnaire. Mutates questionnaires_missing list."""
        if questionnaire.can_retake_after is None:
            questionnaires_missing.append(questionnaire.id)
            return None
        assert submission.submitted_at is not None  # Submissions with evaluations always have submitted_at
        retry_on = submission.submitted_at + questionnaire.can_retake_after
        if retry_on < timezone.now():
            questionnaires_missing.append(questionnaire.id)
            return None
        return EventUserEligibility(
            allowed=False,
            reason=_(Reasons.QUESTIONNAIRE_FAILED),
            event_id=self.event.id,
            next_step=NextStep.WAIT_TO_RETAKE_QUESTIONNAIRE,
            retry_on=retry_on,
        )

    def _check_missing_questionnaires(self) -> EventUserEligibility | None:
        questionnaires_missing = []
        for org_questionnaire in self._get_applicable_questionnaires():
            # Look up the submission in our O(1) map. No database query.
            submissions = self.handler.submission_map.get(org_questionnaire.questionnaire_id)
            if (
                submissions is None
                or submissions[0].status != QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
            ):
                questionnaires_missing.append(org_questionnaire.questionnaire_id)
                continue

            # Check if the submission's approval has expired
            if self._is_submission_expired(org_questionnaire, submissions[0]):
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
        for org_questionnaire in self._get_applicable_questionnaires():
            # Look up the submission in our O(1) map. No database query.
            if submissions := self.handler.submission_map.get(org_questionnaire.questionnaire_id):
                evaluation = getattr(submissions[0], "evaluation", None)
                if (
                    evaluation is None
                    or evaluation.status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW
                ):
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
        failed_questionnaires: list[uuid.UUID] = []
        questionnaires_missing: list[uuid.UUID] = []
        for org_questionnaire in self._get_applicable_questionnaires():
            # Look up the submission in our O(1) map. No database query.
            questionnaire = org_questionnaire.questionnaire
            submissions = self.handler.submission_map.get(questionnaire.id)
            # Skip if no submissions or no evaluation yet
            if not submissions or not (evaluation := getattr(submissions[0], "evaluation", None)):
                continue
            if evaluation.status != QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED:
                continue
            # At this point we have a rejected evaluation
            if 0 < questionnaire.max_attempts <= len(submissions):
                failed_questionnaires.append(org_questionnaire.questionnaire_id)
                continue
            # Check if user can retake
            if result := self._check_retake_eligibility(questionnaire, submissions[0], questionnaires_missing):
                return result
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


class AvailabilityGate(BaseEligibilityGate):
    """Gate #9: Checks if the event has space available for another attendee.

    This is a preliminary capacity check using prefetched data (zero additional DB queries).
    It must use the same counting logic as EventManager._assert_capacity() to avoid
    inconsistencies, but operates on in-memory data for performance.

    The final authoritative capacity check happens in _assert_capacity() within a transaction
    with row-level locking to prevent race conditions.
    """

    def check(self) -> EventUserEligibility | None:
        """Check if the event has space available for another attendee."""
        if self.event.max_attendees == 0 or self.handler.overrides_max_attendees():
            return None

        if self._get_attendee_count() >= self.event.max_attendees:
            return EventUserEligibility(
                allowed=False,
                event_id=self.event.id,
                reason=_(Reasons.EVENT_IS_FULL),
                next_step=self._get_next_step(),
            )
        return None

    def _get_next_step(self) -> NextStep | None:
        if not self.event.waitlist_open:
            return None
        if self.event.user_is_waitlisted:  # type: ignore[attr-defined]
            return NextStep.WAIT_FOR_OPEN_SPOT
        return NextStep.JOIN_WAITLIST

    def _get_attendee_count(self) -> int:
        """Count attendees using prefetched data.

        Uses the same counting logic as EventManager._assert_capacity():
        - For ticket events: count non-cancelled tickets (each ticket = one attendee)
        - For RSVP events: count YES RSVPs

        This operates on prefetched data for performance, while _assert_capacity()
        makes fresh DB queries with locking for race-safety.
        """
        if self.event.requires_ticket:
            return len(
                [ticket for ticket in self.event.tickets.all() if ticket.status != Ticket.TicketStatus.CANCELLED]
            )
        return len([rsvp for rsvp in self.event.rsvps.all() if rsvp.status == EventRSVP.RsvpStatus.YES])


class TicketSalesGate(BaseEligibilityGate):
    """Gate #10: Checks if tickets are currently on sale for ticket-required events."""

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


# List of all gates in execution order
ELIGIBILITY_GATES: list[type[BaseEligibilityGate]] = [
    PrivilegedAccessGate,
    BlacklistGate,
    EventStatusGate,
    RSVPDeadlineGate,
    ApplyDeadlineGate,
    InvitationGate,
    MembershipGate,
    QuestionnaireGate,
    AvailabilityGate,
    TicketSalesGate,
]
