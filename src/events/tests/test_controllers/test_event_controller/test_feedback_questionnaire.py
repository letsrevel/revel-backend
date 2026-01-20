"""Tests for feedback questionnaire endpoints.

Tests cover:
- GET /events/{event_id}/questionnaire/{questionnaire_id} for FEEDBACK type
- POST /events/{event_id}/questionnaire/{questionnaire_id}/submit for FEEDBACK type
- GET /events/{event_id}/my-status including feedback_questionnaires field
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import orjson
import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    Event,
    EventQuestionnaireSubmission,
    EventRSVP,
    Organization,
    OrganizationQuestionnaire,
    Ticket,
    TicketTier,
)
from questionnaires.models import (
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def feedback_test_user(django_user_model: type[RevelUser]) -> RevelUser:
    """User for feedback controller tests."""
    return django_user_model.objects.create_user(
        username="fb_test_user",
        email="fbtest@example.com",
        password="pass",
    )


@pytest.fixture
def feedback_test_client(feedback_test_user: RevelUser) -> Client:
    """API client for feedback test user."""
    refresh = RefreshToken.for_user(feedback_test_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def feedback_test_org(django_user_model: type[RevelUser]) -> Organization:
    """Organization for feedback controller tests."""
    owner = django_user_model.objects.create_user(
        username="fb_test_org_owner",
        email="fbtestowner@example.com",
        password="pass",
    )
    return Organization.objects.create(
        name="Feedback Test Organization",
        slug="feedback-test-organization",
        owner=owner,
    )


@pytest.fixture
def past_event_for_controller(feedback_test_org: Organization) -> Event:
    """A past event for controller tests."""
    return Event.objects.create(
        organization=feedback_test_org,
        name="Past Controller Event",
        slug="past-controller-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status="open",
        start=timezone.now() - timedelta(days=2),
        end=timezone.now() - timedelta(days=1),
        requires_ticket=False,
    )


@pytest.fixture
def future_event_for_controller(feedback_test_org: Organization) -> Event:
    """A future event for controller tests."""
    return Event.objects.create(
        organization=feedback_test_org,
        name="Future Controller Event",
        slug="future-controller-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status="open",
        start=timezone.now() + timedelta(days=1),
        end=timezone.now() + timedelta(days=2),
        requires_ticket=False,
    )


@pytest.fixture
def feedback_questionnaire_for_controller(
    feedback_test_org: Organization, past_event_for_controller: Event
) -> tuple[Questionnaire, OrganizationQuestionnaire]:
    """A feedback questionnaire with MCQ linked to past event."""
    q = Questionnaire.objects.create(
        name="Feedback Controller Questionnaire",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
    )
    mcq = MultipleChoiceQuestion.objects.create(
        questionnaire=q,
        question="How was the event?",
        is_mandatory=True,
    )
    MultipleChoiceOption.objects.create(question=mcq, option="Great", is_correct=True)
    MultipleChoiceOption.objects.create(question=mcq, option="OK", is_correct=False)

    org_q = OrganizationQuestionnaire.objects.create(
        organization=feedback_test_org,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
    )
    org_q.events.add(past_event_for_controller)
    return q, org_q


@pytest.fixture
def admission_questionnaire_for_controller(
    feedback_test_org: Organization, past_event_for_controller: Event
) -> tuple[Questionnaire, OrganizationQuestionnaire]:
    """An admission questionnaire with MCQ linked to past event."""
    q = Questionnaire.objects.create(
        name="Admission Controller Questionnaire",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
    )
    mcq = MultipleChoiceQuestion.objects.create(
        questionnaire=q,
        question="Why do you want to attend?",
        is_mandatory=True,
    )
    MultipleChoiceOption.objects.create(question=mcq, option="Interested", is_correct=True)

    org_q = OrganizationQuestionnaire.objects.create(
        organization=feedback_test_org,
        questionnaire=q,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
    )
    org_q.events.add(past_event_for_controller)
    return q, org_q


# --- GET /events/{event_id}/questionnaire/{questionnaire_id} tests ---


class TestGetFeedbackQuestionnaire:
    """Tests for getting feedback questionnaires."""

    def test_requires_authentication_for_feedback_questionnaire(
        self,
        client: Client,
        past_event_for_controller: Event,
        feedback_questionnaire_for_controller: tuple[Questionnaire, OrganizationQuestionnaire],
    ) -> None:
        """Anonymous users should get 401 for feedback questionnaires."""
        questionnaire, _ = feedback_questionnaire_for_controller
        url = reverse(
            "api:get_questionnaire",
            kwargs={
                "event_id": past_event_for_controller.pk,
                "questionnaire_id": questionnaire.pk,
            },
        )
        response = client.get(url)
        assert response.status_code == 401

    def test_fails_when_event_not_ended(
        self,
        feedback_test_client: Client,
        feedback_test_user: RevelUser,
        future_event_for_controller: Event,
        feedback_test_org: Organization,
    ) -> None:
        """Should return 403 when event hasn't ended yet."""
        q = Questionnaire.objects.create(
            name="Future Feedback Q",
            status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        )
        org_q = OrganizationQuestionnaire.objects.create(
            organization=feedback_test_org,
            questionnaire=q,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        )
        org_q.events.add(future_event_for_controller)

        # User has RSVP
        EventRSVP.objects.create(
            event=future_event_for_controller,
            user=feedback_test_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        url = reverse(
            "api:get_questionnaire",
            kwargs={
                "event_id": future_event_for_controller.pk,
                "questionnaire_id": q.pk,
            },
        )
        response = feedback_test_client.get(url)
        assert response.status_code == 403
        assert "after the event ends" in response.json()["detail"]

    def test_fails_when_user_not_attended(
        self,
        feedback_test_client: Client,
        past_event_for_controller: Event,
        feedback_questionnaire_for_controller: tuple[Questionnaire, OrganizationQuestionnaire],
    ) -> None:
        """Should return 403 when user didn't attend the event."""
        questionnaire, _ = feedback_questionnaire_for_controller
        url = reverse(
            "api:get_questionnaire",
            kwargs={
                "event_id": past_event_for_controller.pk,
                "questionnaire_id": questionnaire.pk,
            },
        )
        response = feedback_test_client.get(url)
        assert response.status_code == 403
        assert "events you attended" in response.json()["detail"]

    def test_succeeds_when_event_ended_and_user_attended(
        self,
        feedback_test_client: Client,
        feedback_test_user: RevelUser,
        past_event_for_controller: Event,
        feedback_questionnaire_for_controller: tuple[Questionnaire, OrganizationQuestionnaire],
    ) -> None:
        """Should return 200 with questionnaire when eligible."""
        questionnaire, _ = feedback_questionnaire_for_controller

        # Add attendance
        EventRSVP.objects.create(
            event=past_event_for_controller,
            user=feedback_test_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        url = reverse(
            "api:get_questionnaire",
            kwargs={
                "event_id": past_event_for_controller.pk,
                "questionnaire_id": questionnaire.pk,
            },
        )
        response = feedback_test_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(questionnaire.pk)

    def test_admission_questionnaire_allows_anonymous(
        self,
        client: Client,
        past_event_for_controller: Event,
        admission_questionnaire_for_controller: tuple[Questionnaire, OrganizationQuestionnaire],
    ) -> None:
        """Anonymous users should be able to get admission questionnaires."""
        questionnaire, _ = admission_questionnaire_for_controller
        # Set event to future so admission is still valid
        past_event_for_controller.start = timezone.now() + timedelta(days=1)
        past_event_for_controller.end = timezone.now() + timedelta(days=2)
        past_event_for_controller.save()

        url = reverse(
            "api:get_questionnaire",
            kwargs={
                "event_id": past_event_for_controller.pk,
                "questionnaire_id": questionnaire.pk,
            },
        )
        response = client.get(url)
        assert response.status_code == 200


# --- POST /events/{event_id}/questionnaire/{questionnaire_id}/submit tests ---


class TestSubmitFeedbackQuestionnaire:
    """Tests for submitting feedback questionnaires."""

    def test_fails_when_event_not_ended(
        self,
        feedback_test_client: Client,
        feedback_test_user: RevelUser,
        future_event_for_controller: Event,
        feedback_test_org: Organization,
    ) -> None:
        """Should return 403 when event hasn't ended yet."""
        q = Questionnaire.objects.create(
            name="Future Feedback Q Submit",
            status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        )
        mcq = MultipleChoiceQuestion.objects.create(
            questionnaire=q,
            question="How was it?",
            is_mandatory=True,
        )
        option = MultipleChoiceOption.objects.create(question=mcq, option="Great", is_correct=True)
        org_q = OrganizationQuestionnaire.objects.create(
            organization=feedback_test_org,
            questionnaire=q,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        )
        org_q.events.add(future_event_for_controller)

        # User has RSVP
        EventRSVP.objects.create(
            event=future_event_for_controller,
            user=feedback_test_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        url = reverse(
            "api:submit_questionnaire",
            kwargs={
                "event_id": future_event_for_controller.pk,
                "questionnaire_id": q.pk,
            },
        )
        payload = {
            "questionnaire_id": str(q.pk),
            "status": "ready",
            "multiple_choice_answers": [{"question_id": str(mcq.pk), "options_id": [str(option.pk)]}],
        }
        response = feedback_test_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 403

    def test_fails_when_user_not_attended(
        self,
        feedback_test_client: Client,
        past_event_for_controller: Event,
        feedback_questionnaire_for_controller: tuple[Questionnaire, OrganizationQuestionnaire],
    ) -> None:
        """Should return 403 when user didn't attend the event."""
        questionnaire, _ = feedback_questionnaire_for_controller
        mcq = questionnaire.multiplechoicequestion_questions.first()
        option = mcq.options.first()  # type: ignore[union-attr]

        url = reverse(
            "api:submit_questionnaire",
            kwargs={
                "event_id": past_event_for_controller.pk,
                "questionnaire_id": questionnaire.pk,
            },
        )
        payload = {
            "questionnaire_id": str(questionnaire.pk),
            "status": "ready",
            "multiple_choice_answers": [
                {"question_id": str(mcq.pk), "options_id": [str(option.pk)]}  # type: ignore[union-attr]
            ],
        }
        response = feedback_test_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 403
        assert "events you attended" in response.json()["detail"]

    @patch("events.controllers.event_public.attendance.evaluate_questionnaire_submission.delay")
    def test_succeeds_and_skips_evaluation(
        self,
        mock_evaluate_task: MagicMock,
        feedback_test_client: Client,
        feedback_test_user: RevelUser,
        past_event_for_controller: Event,
        feedback_questionnaire_for_controller: tuple[Questionnaire, OrganizationQuestionnaire],
    ) -> None:
        """Should submit successfully and NOT trigger evaluation."""
        questionnaire, _ = feedback_questionnaire_for_controller
        mcq = questionnaire.multiplechoicequestion_questions.first()
        option = mcq.options.first()  # type: ignore[union-attr]

        # Add attendance
        EventRSVP.objects.create(
            event=past_event_for_controller,
            user=feedback_test_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        url = reverse(
            "api:submit_questionnaire",
            kwargs={
                "event_id": past_event_for_controller.pk,
                "questionnaire_id": questionnaire.pk,
            },
        )
        payload = {
            "questionnaire_id": str(questionnaire.pk),
            "status": "ready",
            "multiple_choice_answers": [
                {"question_id": str(mcq.pk), "options_id": [str(option.pk)]}  # type: ignore[union-attr]
            ],
        }
        response = feedback_test_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        assert QuestionnaireSubmission.objects.count() == 1
        # Evaluation should NOT be triggered for feedback questionnaires
        mock_evaluate_task.assert_not_called()

    @patch("events.controllers.event_public.attendance.evaluate_questionnaire_submission.delay")
    def test_feedback_does_not_check_deadline(
        self,
        mock_evaluate_task: MagicMock,
        feedback_test_client: Client,
        feedback_test_user: RevelUser,
        past_event_for_controller: Event,
        feedback_questionnaire_for_controller: tuple[Questionnaire, OrganizationQuestionnaire],
    ) -> None:
        """Feedback questionnaires should not check apply_before deadline."""
        questionnaire, _ = feedback_questionnaire_for_controller
        mcq = questionnaire.multiplechoicequestion_questions.first()
        option = mcq.options.first()  # type: ignore[union-attr]

        # Set deadline in the past (would block admission questionnaires)
        past_event_for_controller.apply_before = timezone.now() - timedelta(days=10)
        past_event_for_controller.save()

        # Add attendance
        EventRSVP.objects.create(
            event=past_event_for_controller,
            user=feedback_test_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        url = reverse(
            "api:submit_questionnaire",
            kwargs={
                "event_id": past_event_for_controller.pk,
                "questionnaire_id": questionnaire.pk,
            },
        )
        payload = {
            "questionnaire_id": str(questionnaire.pk),
            "status": "ready",
            "multiple_choice_answers": [
                {"question_id": str(mcq.pk), "options_id": [str(option.pk)]}  # type: ignore[union-attr]
            ],
        }
        response = feedback_test_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        # Should succeed because deadline check is skipped for feedback
        assert response.status_code == 200

    @patch("events.controllers.event_public.attendance.evaluate_questionnaire_submission.delay")
    def test_creates_feedback_submission_record(
        self,
        mock_evaluate_task: MagicMock,
        feedback_test_client: Client,
        feedback_test_user: RevelUser,
        past_event_for_controller: Event,
        feedback_questionnaire_for_controller: tuple[Questionnaire, OrganizationQuestionnaire],
    ) -> None:
        """Should create EventQuestionnaireSubmission record on successful submission."""
        questionnaire, _ = feedback_questionnaire_for_controller
        mcq = questionnaire.multiplechoicequestion_questions.first()
        option = mcq.options.first()  # type: ignore[union-attr]

        # Add attendance
        EventRSVP.objects.create(
            event=past_event_for_controller,
            user=feedback_test_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        url = reverse(
            "api:submit_questionnaire",
            kwargs={
                "event_id": past_event_for_controller.pk,
                "questionnaire_id": questionnaire.pk,
            },
        )
        payload = {
            "questionnaire_id": str(questionnaire.pk),
            "status": "ready",
            "multiple_choice_answers": [
                {"question_id": str(mcq.pk), "options_id": [str(option.pk)]}  # type: ignore[union-attr]
            ],
        }

        response = feedback_test_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 200

        # Check EventQuestionnaireSubmission was created
        assert EventQuestionnaireSubmission.objects.filter(
            event=past_event_for_controller,
            user=feedback_test_user,
            questionnaire=questionnaire,
        ).exists()

    @patch("events.controllers.event_public.attendance.evaluate_questionnaire_submission.delay")
    def test_second_submission_fails_with_403(
        self,
        mock_evaluate_task: MagicMock,
        feedback_test_client: Client,
        feedback_test_user: RevelUser,
        past_event_for_controller: Event,
        feedback_questionnaire_for_controller: tuple[Questionnaire, OrganizationQuestionnaire],
    ) -> None:
        """Second feedback submission should fail with 403."""
        questionnaire, _ = feedback_questionnaire_for_controller
        mcq = questionnaire.multiplechoicequestion_questions.first()
        option = mcq.options.first()  # type: ignore[union-attr]

        # Add attendance
        EventRSVP.objects.create(
            event=past_event_for_controller,
            user=feedback_test_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        url = reverse(
            "api:submit_questionnaire",
            kwargs={
                "event_id": past_event_for_controller.pk,
                "questionnaire_id": questionnaire.pk,
            },
        )
        payload = {
            "questionnaire_id": str(questionnaire.pk),
            "status": "ready",
            "multiple_choice_answers": [
                {"question_id": str(mcq.pk), "options_id": [str(option.pk)]}  # type: ignore[union-attr]
            ],
        }

        # First submission
        response1 = feedback_test_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response1.status_code == 200

        # Second submission should fail with 403
        response2 = feedback_test_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response2.status_code == 403
        assert "already submitted" in response2.json()["detail"]

    @patch("events.controllers.event_public.attendance.evaluate_questionnaire_submission.delay")
    def test_draft_submission_does_not_create_record(
        self,
        mock_evaluate_task: MagicMock,
        feedback_test_client: Client,
        feedback_test_user: RevelUser,
        past_event_for_controller: Event,
        feedback_questionnaire_for_controller: tuple[Questionnaire, OrganizationQuestionnaire],
    ) -> None:
        """Draft submissions should NOT create EventQuestionnaireSubmission record."""
        questionnaire, _ = feedback_questionnaire_for_controller
        mcq = questionnaire.multiplechoicequestion_questions.first()
        option = mcq.options.first()  # type: ignore[union-attr]

        # Add attendance
        EventRSVP.objects.create(
            event=past_event_for_controller,
            user=feedback_test_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        url = reverse(
            "api:submit_questionnaire",
            kwargs={
                "event_id": past_event_for_controller.pk,
                "questionnaire_id": questionnaire.pk,
            },
        )
        payload = {
            "questionnaire_id": str(questionnaire.pk),
            "status": "draft",  # Draft, not ready
            "multiple_choice_answers": [
                {"question_id": str(mcq.pk), "options_id": [str(option.pk)]}  # type: ignore[union-attr]
            ],
        }

        response = feedback_test_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 200

        # Should NOT create EventQuestionnaireSubmission for draft
        assert not EventQuestionnaireSubmission.objects.filter(
            event=past_event_for_controller,
            user=feedback_test_user,
            questionnaire=questionnaire,
        ).exists()


# --- GET /events/{event_id}/my-status tests ---


class TestMyStatusFeedbackQuestionnaires:
    """Tests for feedback_questionnaires field in my-status endpoint."""

    def test_returns_empty_when_event_not_ended(
        self,
        feedback_test_client: Client,
        feedback_test_user: RevelUser,
        future_event_for_controller: Event,
        feedback_test_org: Organization,
    ) -> None:
        """Should return empty feedback_questionnaires when event not ended."""
        q = Questionnaire.objects.create(
            name="Future Status Feedback Q",
            status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        )
        org_q = OrganizationQuestionnaire.objects.create(
            organization=feedback_test_org,
            questionnaire=q,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        )
        org_q.events.add(future_event_for_controller)

        # User has RSVP
        EventRSVP.objects.create(
            event=future_event_for_controller,
            user=feedback_test_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        url = reverse(
            "api:get_my_event_status",
            kwargs={"event_id": future_event_for_controller.pk},
        )
        response = feedback_test_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data.get("feedback_questionnaires") == []

    def test_returns_empty_when_user_not_attended(
        self,
        feedback_test_client: Client,
        past_event_for_controller: Event,
        feedback_questionnaire_for_controller: tuple[Questionnaire, OrganizationQuestionnaire],
    ) -> None:
        """Should return empty when user didn't attend (returns eligibility)."""
        url = reverse(
            "api:get_my_event_status",
            kwargs={"event_id": past_event_for_controller.pk},
        )
        response = feedback_test_client.get(url)
        assert response.status_code == 200
        data = response.json()
        # User has no RSVP/ticket, so gets EventUserEligibility response
        # which doesn't have feedback_questionnaires field
        assert "allowed" in data  # This is EventUserEligibility

    def test_returns_questionnaire_ids_when_eligible(
        self,
        feedback_test_client: Client,
        feedback_test_user: RevelUser,
        past_event_for_controller: Event,
        feedback_questionnaire_for_controller: tuple[Questionnaire, OrganizationQuestionnaire],
    ) -> None:
        """Should return questionnaire IDs when user attended and event ended."""
        questionnaire, _ = feedback_questionnaire_for_controller

        # Add attendance
        EventRSVP.objects.create(
            event=past_event_for_controller,
            user=feedback_test_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        url = reverse(
            "api:get_my_event_status",
            kwargs={"event_id": past_event_for_controller.pk},
        )
        response = feedback_test_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert "feedback_questionnaires" in data
        assert str(questionnaire.pk) in data["feedback_questionnaires"]

    def test_returns_questionnaire_ids_with_ticket(
        self,
        feedback_test_client: Client,
        feedback_test_user: RevelUser,
        feedback_test_org: Organization,
    ) -> None:
        """Should return questionnaire IDs when user has active ticket."""
        # Create ticketed event
        event = Event.objects.create(
            organization=feedback_test_org,
            name="Ticketed Past Event",
            slug="ticketed-past-event",
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
            user=feedback_test_user,
            tier=tier,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name=feedback_test_user.username,
        )

        # Create feedback questionnaire
        q = Questionnaire.objects.create(
            name="Ticketed Feedback Q",
            status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        )
        org_q = OrganizationQuestionnaire.objects.create(
            organization=feedback_test_org,
            questionnaire=q,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        )
        org_q.events.add(event)

        url = reverse(
            "api:get_my_event_status",
            kwargs={"event_id": event.pk},
        )
        response = feedback_test_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert "feedback_questionnaires" in data
        assert str(q.pk) in data["feedback_questionnaires"]

    def test_excludes_already_submitted_questionnaires(
        self,
        feedback_test_client: Client,
        feedback_test_user: RevelUser,
        past_event_for_controller: Event,
        feedback_questionnaire_for_controller: tuple[Questionnaire, OrganizationQuestionnaire],
    ) -> None:
        """Should exclude questionnaires user has already submitted."""
        questionnaire, _ = feedback_questionnaire_for_controller

        # Add attendance
        EventRSVP.objects.create(
            event=past_event_for_controller,
            user=feedback_test_user,
            status=EventRSVP.RsvpStatus.YES,
        )

        # Create existing feedback submission
        submission = QuestionnaireSubmission.objects.create(
            questionnaire=questionnaire,
            user=feedback_test_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        EventQuestionnaireSubmission.objects.create(
            event=past_event_for_controller,
            user=feedback_test_user,
            questionnaire=questionnaire,
            submission=submission,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
        )

        url = reverse(
            "api:get_my_event_status",
            kwargs={"event_id": past_event_for_controller.pk},
        )
        response = feedback_test_client.get(url)
        assert response.status_code == 200
        data = response.json()
        # Questionnaire should be excluded from the list
        assert "feedback_questionnaires" in data
        assert str(questionnaire.pk) not in data["feedback_questionnaires"]
