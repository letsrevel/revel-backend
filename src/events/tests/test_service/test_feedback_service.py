"""Tests for feedback_service module.

Tests cover:
- User attendance checking (RSVP YES or active/checked-in ticket)
- Feedback questionnaire access validation (event ended + user attended)
- Getting feedback questionnaires for users
- Preventing evaluation of feedback questionnaires
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import (
    Event,
    EventFeedbackSubmission,
    EventRSVP,
    Organization,
    OrganizationQuestionnaire,
    Ticket,
    TicketTier,
)
from events.service import feedback_service
from questionnaires.models import Questionnaire, QuestionnaireSubmission

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def feedback_user(django_user_model: type[RevelUser]) -> RevelUser:
    """User for feedback testing."""
    return django_user_model.objects.create_user(
        username="feedback_user",
        email="feedback@example.com",
        password="pass",
    )


@pytest.fixture
def feedback_org(django_user_model: type[RevelUser]) -> Organization:
    """Organization for feedback testing."""
    owner = django_user_model.objects.create_user(
        username="feedback_org_owner",
        email="feedbackowner@example.com",
        password="pass",
    )
    return Organization.objects.create(
        name="Feedback Test Org",
        slug="feedback-test-org",
        owner=owner,
    )


@pytest.fixture
def past_event(feedback_org: Organization) -> Event:
    """An event that has already ended."""
    return Event.objects.create(
        organization=feedback_org,
        name="Past Event",
        slug="past-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status="open",
        start=timezone.now() - timedelta(days=2),
        end=timezone.now() - timedelta(days=1),
        requires_ticket=False,
    )


@pytest.fixture
def future_event(feedback_org: Organization) -> Event:
    """An event that hasn't ended yet."""
    return Event.objects.create(
        organization=feedback_org,
        name="Future Event",
        slug="future-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status="open",
        start=timezone.now() + timedelta(days=1),
        end=timezone.now() + timedelta(days=2),
        requires_ticket=False,
    )


@pytest.fixture
def ongoing_event(feedback_org: Organization) -> Event:
    """An event that is currently ongoing."""
    return Event.objects.create(
        organization=feedback_org,
        name="Ongoing Event",
        slug="ongoing-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status="open",
        start=timezone.now() - timedelta(hours=1),
        end=timezone.now() + timedelta(hours=23),
        requires_ticket=False,
    )


@pytest.fixture
def feedback_questionnaire(feedback_org: Organization) -> Questionnaire:
    """A FEEDBACK type questionnaire."""
    q = Questionnaire.objects.create(
        name="Feedback Questionnaire",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
    )
    return q


@pytest.fixture
def feedback_org_questionnaire(
    feedback_org: Organization, feedback_questionnaire: Questionnaire, past_event: Event
) -> OrganizationQuestionnaire:
    """An OrganizationQuestionnaire of FEEDBACK type linked to past_event."""
    org_q = OrganizationQuestionnaire.objects.create(
        organization=feedback_org,
        questionnaire=feedback_questionnaire,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
    )
    org_q.events.add(past_event)
    return org_q


@pytest.fixture
def admission_org_questionnaire(feedback_org: Organization, past_event: Event) -> OrganizationQuestionnaire:
    """An OrganizationQuestionnaire of ADMISSION type linked to past_event."""
    q = Questionnaire.objects.create(
        name="Admission Questionnaire",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
    )
    org_q = OrganizationQuestionnaire.objects.create(
        organization=feedback_org,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
    )
    org_q.events.add(past_event)
    return org_q


# --- user_attended_event tests ---


class TestUserAttendedEvent:
    """Tests for user_attended_event function."""

    def test_returns_true_with_yes_rsvp(
        self,
        past_event: Event,
        feedback_user: RevelUser,
    ) -> None:
        """Should return True when user has RSVP YES."""
        EventRSVP.objects.create(
            event=past_event,
            user=feedback_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        assert feedback_service.user_attended_event(feedback_user, past_event) is True

    def test_returns_false_with_no_rsvp(
        self,
        past_event: Event,
        feedback_user: RevelUser,
    ) -> None:
        """Should return False when user has RSVP NO."""
        EventRSVP.objects.create(
            event=past_event,
            user=feedback_user,
            status=EventRSVP.RsvpStatus.NO,
        )

        assert feedback_service.user_attended_event(feedback_user, past_event) is False

    def test_returns_false_with_maybe_rsvp(
        self,
        past_event: Event,
        feedback_user: RevelUser,
    ) -> None:
        """Should return False when user has RSVP MAYBE."""
        EventRSVP.objects.create(
            event=past_event,
            user=feedback_user,
            status=EventRSVP.RsvpStatus.MAYBE,
        )

        assert feedback_service.user_attended_event(feedback_user, past_event) is False

    def test_returns_true_with_active_ticket(
        self,
        feedback_org: Organization,
        feedback_user: RevelUser,
    ) -> None:
        """Should return True when user has an ACTIVE ticket."""
        event = Event.objects.create(
            organization=feedback_org,
            name="Ticketed Event",
            slug="ticketed-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            start=timezone.now() - timedelta(days=2),
            end=timezone.now() - timedelta(days=1),
            requires_ticket=True,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="General",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        Ticket.objects.create(
            event=event,
            user=feedback_user,
            tier=tier,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name=feedback_user.username,
        )

        assert feedback_service.user_attended_event(feedback_user, event) is True

    def test_returns_true_with_checked_in_ticket(
        self,
        feedback_org: Organization,
        feedback_user: RevelUser,
    ) -> None:
        """Should return True when user has a CHECKED_IN ticket."""
        event = Event.objects.create(
            organization=feedback_org,
            name="Ticketed Event 2",
            slug="ticketed-event-2",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            start=timezone.now() - timedelta(days=2),
            end=timezone.now() - timedelta(days=1),
            requires_ticket=True,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="General",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        Ticket.objects.create(
            event=event,
            user=feedback_user,
            tier=tier,
            status=Ticket.TicketStatus.CHECKED_IN,
            guest_name=feedback_user.username,
        )

        assert feedback_service.user_attended_event(feedback_user, event) is True

    def test_returns_false_with_pending_ticket(
        self,
        feedback_org: Organization,
        feedback_user: RevelUser,
    ) -> None:
        """Should return False when user has only a PENDING ticket."""
        event = Event.objects.create(
            organization=feedback_org,
            name="Ticketed Event 3",
            slug="ticketed-event-3",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            start=timezone.now() - timedelta(days=2),
            end=timezone.now() - timedelta(days=1),
            requires_ticket=True,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="General",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        Ticket.objects.create(
            event=event,
            user=feedback_user,
            tier=tier,
            status=Ticket.TicketStatus.PENDING,
            guest_name=feedback_user.username,
        )

        assert feedback_service.user_attended_event(feedback_user, event) is False

    def test_returns_false_with_cancelled_ticket(
        self,
        feedback_org: Organization,
        feedback_user: RevelUser,
    ) -> None:
        """Should return False when user has only a CANCELLED ticket."""
        event = Event.objects.create(
            organization=feedback_org,
            name="Ticketed Event 4",
            slug="ticketed-event-4",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            start=timezone.now() - timedelta(days=2),
            end=timezone.now() - timedelta(days=1),
            requires_ticket=True,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="General",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        Ticket.objects.create(
            event=event,
            user=feedback_user,
            tier=tier,
            status=Ticket.TicketStatus.CANCELLED,
            guest_name=feedback_user.username,
        )

        assert feedback_service.user_attended_event(feedback_user, event) is False

    def test_returns_false_with_no_interaction(
        self,
        past_event: Event,
        feedback_user: RevelUser,
    ) -> None:
        """Should return False when user has no RSVP or ticket."""
        assert feedback_service.user_attended_event(feedback_user, past_event) is False


# --- validate_feedback_questionnaire_access tests ---


class TestValidateFeedbackQuestionnaireAccess:
    """Tests for validate_feedback_questionnaire_access function."""

    def test_passes_for_non_feedback_questionnaire(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        admission_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Should not raise for ADMISSION type questionnaires."""
        # User hasn't attended, but it's an ADMISSION questionnaire so it should pass
        feedback_service.validate_feedback_questionnaire_access(feedback_user, past_event, admission_org_questionnaire)

    def test_raises_when_event_not_ended(
        self,
        future_event: Event,
        feedback_user: RevelUser,
        feedback_org: Organization,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should raise 403 when event hasn't ended yet."""
        org_q = OrganizationQuestionnaire.objects.create(
            organization=feedback_org,
            questionnaire=feedback_questionnaire,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        )
        org_q.events.add(future_event)

        # Add attendance
        EventRSVP.objects.create(
            event=future_event,
            user=feedback_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        with pytest.raises(HttpError) as exc_info:
            feedback_service.validate_feedback_questionnaire_access(feedback_user, future_event, org_q)
        assert exc_info.value.status_code == 403
        assert "after the event ends" in str(exc_info.value.message)

    def test_raises_when_event_ongoing(
        self,
        ongoing_event: Event,
        feedback_user: RevelUser,
        feedback_org: Organization,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should raise 403 when event is still ongoing."""
        org_q = OrganizationQuestionnaire.objects.create(
            organization=feedback_org,
            questionnaire=feedback_questionnaire,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        )
        org_q.events.add(ongoing_event)

        # Add attendance
        EventRSVP.objects.create(
            event=ongoing_event,
            user=feedback_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        with pytest.raises(HttpError) as exc_info:
            feedback_service.validate_feedback_questionnaire_access(feedback_user, ongoing_event, org_q)
        assert exc_info.value.status_code == 403
        assert "after the event ends" in str(exc_info.value.message)

    def test_raises_when_user_not_attended(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        feedback_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Should raise 403 when user didn't attend the event."""
        with pytest.raises(HttpError) as exc_info:
            feedback_service.validate_feedback_questionnaire_access(
                feedback_user, past_event, feedback_org_questionnaire
            )
        assert exc_info.value.status_code == 403
        assert "events you attended" in str(exc_info.value.message)

    def test_passes_when_event_ended_and_user_attended_with_rsvp(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        feedback_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Should pass when event ended and user has RSVP YES."""
        EventRSVP.objects.create(
            event=past_event,
            user=feedback_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        # Should not raise
        feedback_service.validate_feedback_questionnaire_access(feedback_user, past_event, feedback_org_questionnaire)

    def test_passes_when_event_ended_and_user_attended_with_ticket(
        self,
        feedback_org: Organization,
        feedback_user: RevelUser,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should pass when event ended and user has active ticket."""
        event = Event.objects.create(
            organization=feedback_org,
            name="Past Ticketed Event",
            slug="past-ticketed-event",
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
            status="open",
            start=timezone.now() - timedelta(days=2),
            end=timezone.now() - timedelta(days=1),
            requires_ticket=True,
        )
        tier = TicketTier.objects.create(
            event=event,
            name="General",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        Ticket.objects.create(
            event=event,
            user=feedback_user,
            tier=tier,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name=feedback_user.username,
        )
        org_q = OrganizationQuestionnaire.objects.create(
            organization=feedback_org,
            questionnaire=feedback_questionnaire,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        )
        org_q.events.add(event)

        # Should not raise
        feedback_service.validate_feedback_questionnaire_access(feedback_user, event, org_q)


# --- get_feedback_questionnaires_for_user tests ---


class TestGetFeedbackQuestionnairesForUser:
    """Tests for get_feedback_questionnaires_for_user function."""

    def test_returns_empty_when_event_not_ended(
        self,
        future_event: Event,
        feedback_user: RevelUser,
        feedback_org: Organization,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should return empty list when event hasn't ended."""
        org_q = OrganizationQuestionnaire.objects.create(
            organization=feedback_org,
            questionnaire=feedback_questionnaire,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        )
        org_q.events.add(future_event)

        # Add attendance
        EventRSVP.objects.create(
            event=future_event,
            user=feedback_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        result = feedback_service.get_feedback_questionnaires_for_user(future_event, feedback_user)

        assert result == []

    def test_returns_empty_when_user_not_attended(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        feedback_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Should return empty list when user didn't attend."""
        result = feedback_service.get_feedback_questionnaires_for_user(past_event, feedback_user)

        assert result == []

    def test_returns_questionnaire_ids_when_eligible(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        feedback_org_questionnaire: OrganizationQuestionnaire,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should return questionnaire IDs when user is eligible."""
        EventRSVP.objects.create(
            event=past_event,
            user=feedback_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        result = feedback_service.get_feedback_questionnaires_for_user(past_event, feedback_user)

        assert result == [feedback_questionnaire.id]

    def test_returns_multiple_questionnaire_ids(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        feedback_org: Organization,
        feedback_org_questionnaire: OrganizationQuestionnaire,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should return multiple questionnaire IDs when multiple exist."""
        # Add attendance
        EventRSVP.objects.create(
            event=past_event,
            user=feedback_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        # Create another feedback questionnaire
        q2 = Questionnaire.objects.create(
            name="Second Feedback",
            status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        )
        org_q2 = OrganizationQuestionnaire.objects.create(
            organization=feedback_org,
            questionnaire=q2,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        )
        org_q2.events.add(past_event)

        result = feedback_service.get_feedback_questionnaires_for_user(past_event, feedback_user)

        assert len(result) == 2
        assert feedback_questionnaire.id in result
        assert q2.id in result

    def test_excludes_admission_questionnaires(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        feedback_org_questionnaire: OrganizationQuestionnaire,
        admission_org_questionnaire: OrganizationQuestionnaire,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should only return FEEDBACK type questionnaires."""
        EventRSVP.objects.create(
            event=past_event,
            user=feedback_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        result = feedback_service.get_feedback_questionnaires_for_user(past_event, feedback_user)

        assert result == [feedback_questionnaire.id]
        assert admission_org_questionnaire.questionnaire_id not in result


# --- validate_not_feedback_questionnaire_for_evaluation tests ---


class TestValidateNotFeedbackQuestionnaireForEvaluation:
    """Tests for validate_not_feedback_questionnaire_for_evaluation function."""

    def test_raises_for_feedback_questionnaire(
        self,
        feedback_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Should raise 400 when questionnaire is FEEDBACK type."""
        with pytest.raises(HttpError) as exc_info:
            feedback_service.validate_not_feedback_questionnaire_for_evaluation(feedback_org_questionnaire)
        assert exc_info.value.status_code == 400
        assert "cannot be evaluated" in str(exc_info.value.message)

    def test_passes_for_admission_questionnaire(
        self,
        admission_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Should not raise for ADMISSION type questionnaires."""
        feedback_service.validate_not_feedback_questionnaire_for_evaluation(admission_org_questionnaire)


# --- user_already_submitted_feedback tests ---


class TestUserAlreadySubmittedFeedback:
    """Tests for user_already_submitted_feedback function."""

    def test_returns_false_when_no_submission(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should return False when user has no feedback submission."""
        result = feedback_service.user_already_submitted_feedback(feedback_user, past_event, feedback_questionnaire)
        assert result is False

    def test_returns_true_when_submission_exists(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should return True when user has existing feedback submission."""
        submission = QuestionnaireSubmission.objects.create(
            questionnaire=feedback_questionnaire,
            user=feedback_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        EventFeedbackSubmission.objects.create(
            event=past_event,
            user=feedback_user,
            questionnaire=feedback_questionnaire,
            submission=submission,
        )

        result = feedback_service.user_already_submitted_feedback(feedback_user, past_event, feedback_questionnaire)
        assert result is True

    def test_returns_false_for_different_event(
        self,
        past_event: Event,
        future_event: Event,
        feedback_user: RevelUser,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should return False when submission is for a different event."""
        submission = QuestionnaireSubmission.objects.create(
            questionnaire=feedback_questionnaire,
            user=feedback_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        EventFeedbackSubmission.objects.create(
            event=future_event,  # Different event
            user=feedback_user,
            questionnaire=feedback_questionnaire,
            submission=submission,
        )

        result = feedback_service.user_already_submitted_feedback(feedback_user, past_event, feedback_questionnaire)
        assert result is False

    def test_returns_false_for_different_questionnaire(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        feedback_questionnaire: Questionnaire,
        feedback_org: Organization,
    ) -> None:
        """Should return False when submission is for a different questionnaire."""
        other_q = Questionnaire.objects.create(
            name="Other Feedback",
            status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        )
        submission = QuestionnaireSubmission.objects.create(
            questionnaire=other_q,
            user=feedback_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        EventFeedbackSubmission.objects.create(
            event=past_event,
            user=feedback_user,
            questionnaire=other_q,  # Different questionnaire
            submission=submission,
        )

        result = feedback_service.user_already_submitted_feedback(feedback_user, past_event, feedback_questionnaire)
        assert result is False


# --- create_feedback_submission_record tests ---


class TestCreateFeedbackSubmissionRecord:
    """Tests for create_feedback_submission_record function."""

    def test_creates_record(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should create a new EventFeedbackSubmission record."""
        submission = QuestionnaireSubmission.objects.create(
            questionnaire=feedback_questionnaire,
            user=feedback_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )

        record, created = feedback_service.create_feedback_submission_record(
            feedback_user, past_event, feedback_questionnaire, submission
        )

        assert created is True
        assert record.event == past_event
        assert record.user == feedback_user
        assert record.questionnaire == feedback_questionnaire
        assert record.submission == submission

    def test_returns_existing_record_on_duplicate(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should return existing record without creating duplicate."""
        submission1 = QuestionnaireSubmission.objects.create(
            questionnaire=feedback_questionnaire,
            user=feedback_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        existing_record = EventFeedbackSubmission.objects.create(
            event=past_event,
            user=feedback_user,
            questionnaire=feedback_questionnaire,
            submission=submission1,
        )

        # Try to create another submission for the same event/user/questionnaire
        submission2 = QuestionnaireSubmission.objects.create(
            questionnaire=feedback_questionnaire,
            user=feedback_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )

        record, created = feedback_service.create_feedback_submission_record(
            feedback_user, past_event, feedback_questionnaire, submission2
        )

        assert created is False
        assert record.id == existing_record.id
        assert record.submission == submission1  # Original submission


# --- validate_feedback_questionnaire_access (already_submitted) tests ---


class TestValidateFeedbackQuestionnaireAccessAlreadySubmitted:
    """Tests for validate_feedback_questionnaire_access with already_submitted check."""

    def test_raises_when_already_submitted(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        feedback_org_questionnaire: OrganizationQuestionnaire,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should raise 403 when user has already submitted feedback."""
        # Add attendance
        EventRSVP.objects.create(
            event=past_event,
            user=feedback_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        # Create existing submission
        submission = QuestionnaireSubmission.objects.create(
            questionnaire=feedback_questionnaire,
            user=feedback_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        EventFeedbackSubmission.objects.create(
            event=past_event,
            user=feedback_user,
            questionnaire=feedback_questionnaire,
            submission=submission,
        )

        with pytest.raises(HttpError) as exc_info:
            feedback_service.validate_feedback_questionnaire_access(
                feedback_user, past_event, feedback_org_questionnaire
            )
        assert exc_info.value.status_code == 403
        assert "already submitted" in str(exc_info.value.message)

    def test_passes_when_check_already_submitted_is_false(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        feedback_org_questionnaire: OrganizationQuestionnaire,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should pass when check_already_submitted is False."""
        # Add attendance
        EventRSVP.objects.create(
            event=past_event,
            user=feedback_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        # Create existing submission
        submission = QuestionnaireSubmission.objects.create(
            questionnaire=feedback_questionnaire,
            user=feedback_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        EventFeedbackSubmission.objects.create(
            event=past_event,
            user=feedback_user,
            questionnaire=feedback_questionnaire,
            submission=submission,
        )

        # Should not raise with check_already_submitted=False
        feedback_service.validate_feedback_questionnaire_access(
            feedback_user, past_event, feedback_org_questionnaire, check_already_submitted=False
        )


# --- get_feedback_questionnaires_for_user (excludes already submitted) tests ---


class TestGetFeedbackQuestionnairesExcludesSubmitted:
    """Tests for get_feedback_questionnaires_for_user excluding already submitted."""

    def test_excludes_already_submitted_questionnaire(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        feedback_org_questionnaire: OrganizationQuestionnaire,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should exclude questionnaires user has already submitted."""
        EventRSVP.objects.create(
            event=past_event,
            user=feedback_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        # Create submission for the feedback questionnaire
        submission = QuestionnaireSubmission.objects.create(
            questionnaire=feedback_questionnaire,
            user=feedback_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        EventFeedbackSubmission.objects.create(
            event=past_event,
            user=feedback_user,
            questionnaire=feedback_questionnaire,
            submission=submission,
        )

        result = feedback_service.get_feedback_questionnaires_for_user(past_event, feedback_user)

        assert result == []

    def test_returns_unsubmitted_questionnaires(
        self,
        past_event: Event,
        feedback_user: RevelUser,
        feedback_org: Organization,
        feedback_org_questionnaire: OrganizationQuestionnaire,
        feedback_questionnaire: Questionnaire,
    ) -> None:
        """Should return questionnaires user has not submitted."""
        EventRSVP.objects.create(
            event=past_event,
            user=feedback_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        # Create submission for the first questionnaire
        submission = QuestionnaireSubmission.objects.create(
            questionnaire=feedback_questionnaire,
            user=feedback_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        EventFeedbackSubmission.objects.create(
            event=past_event,
            user=feedback_user,
            questionnaire=feedback_questionnaire,
            submission=submission,
        )

        # Create another feedback questionnaire (not yet submitted)
        q2 = Questionnaire.objects.create(
            name="Second Feedback",
            status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        )
        org_q2 = OrganizationQuestionnaire.objects.create(
            organization=feedback_org,
            questionnaire=q2,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        )
        org_q2.events.add(past_event)

        result = feedback_service.get_feedback_questionnaires_for_user(past_event, feedback_user)

        assert result == [q2.id]
