"""Tests for event_questionnaire_service module.

Tests cover:
- source_event metadata construction
- Atomic transaction behavior
- READY vs DRAFT submission handling for EventQuestionnaireSubmission records
- Race condition protection for concurrent submissions

Note: Basic submission flow and access validation are covered by controller tests.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, EventQuestionnaireSubmission, Organization, OrganizationQuestionnaire
from events.service import event_questionnaire_service
from questionnaires.models import (
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSubmission,
)
from questionnaires.schema import MultipleChoiceSubmissionSchema, QuestionnaireSubmissionSchema
from questionnaires.service.questionnaire_service import QuestionnaireService

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def eq_user(django_user_model: type[RevelUser]) -> RevelUser:
    """User for event questionnaire testing."""
    return django_user_model.objects.create_user(
        username="eq_test_user",
        email="eqtest@example.com",
        password="pass",
    )


@pytest.fixture
def eq_org(django_user_model: type[RevelUser]) -> Organization:
    """Organization for event questionnaire testing."""
    owner = django_user_model.objects.create_user(
        username="eq_org_owner",
        email="eqorgowner@example.com",
        password="pass",
    )
    return Organization.objects.create(
        name="EQ Test Org",
        slug="eq-test-org",
        owner=owner,
    )


@pytest.fixture
def eq_event(eq_org: Organization) -> Event:
    """An event with a specific start time for metadata testing."""
    return Event.objects.create(
        organization=eq_org,
        name="Test Event For Questionnaire",
        slug="test-event-questionnaire",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status="open",
        start=timezone.now() - timedelta(days=2),
        end=timezone.now() - timedelta(days=1),
        requires_ticket=False,
    )


@pytest.fixture
def eq_questionnaire() -> Questionnaire:
    """A questionnaire for testing."""
    return Questionnaire.objects.create(
        name="EQ Test Questionnaire",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
    )


@pytest.fixture
def eq_mcq(eq_questionnaire: Questionnaire) -> MultipleChoiceQuestion:
    """A multiple choice question with options."""
    return MultipleChoiceQuestion.objects.create(
        questionnaire=eq_questionnaire,
        question="Test question?",
        is_mandatory=True,
        order=1,
    )


@pytest.fixture
def eq_option(eq_mcq: MultipleChoiceQuestion) -> MultipleChoiceOption:
    """An option for the MCQ."""
    return MultipleChoiceOption.objects.create(
        question=eq_mcq,
        option="Test Option",
        is_correct=True,
    )


@pytest.fixture
def eq_org_questionnaire(
    eq_org: Organization,
    eq_questionnaire: Questionnaire,
    eq_event: Event,
) -> OrganizationQuestionnaire:
    """Link questionnaire to organization and event."""
    org_q = OrganizationQuestionnaire.objects.create(
        organization=eq_org,
        questionnaire=eq_questionnaire,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
    )
    org_q.events.add(eq_event)
    return org_q


@pytest.fixture
def eq_questionnaire_service(
    eq_questionnaire: Questionnaire,
    eq_mcq: MultipleChoiceQuestion,  # Ensure MCQ exists before service prefetches
) -> QuestionnaireService:
    """QuestionnaireService instance for the test questionnaire."""
    return QuestionnaireService(eq_questionnaire.id)


# --- source_event metadata tests ---


class TestSourceEventMetadata:
    """Tests for source_event metadata construction in submission."""

    def test_source_event_metadata_format(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire_service: QuestionnaireService,
        eq_org_questionnaire: OrganizationQuestionnaire,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """Verify source_event metadata contains correct event information."""
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=eq_org_questionnaire.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        result = event_questionnaire_service.submit_event_questionnaire(
            user=eq_user,
            event=eq_event,
            questionnaire_service=eq_questionnaire_service,
            org_questionnaire=eq_org_questionnaire,
            submission_schema=submission_schema,
        )

        # Verify metadata structure
        assert result.metadata is not None
        assert "source_event" in result.metadata
        source_event = result.metadata["source_event"]

        assert source_event["event_id"] == str(eq_event.id)
        assert source_event["event_name"] == eq_event.name
        assert source_event["event_start"] == eq_event.start.isoformat()


# --- EventQuestionnaireSubmission creation tests ---


class TestEventQuestionnaireSubmissionCreation:
    """Tests for EventQuestionnaireSubmission record creation logic."""

    def test_draft_submission_does_not_create_tracking_record(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire_service: QuestionnaireService,
        eq_org_questionnaire: OrganizationQuestionnaire,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """Draft submissions should NOT create EventQuestionnaireSubmission records."""
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=eq_org_questionnaire.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        event_questionnaire_service.submit_event_questionnaire(
            user=eq_user,
            event=eq_event,
            questionnaire_service=eq_questionnaire_service,
            org_questionnaire=eq_org_questionnaire,
            submission_schema=submission_schema,
        )

        assert not EventQuestionnaireSubmission.objects.filter(
            user=eq_user,
            event=eq_event,
            questionnaire=eq_org_questionnaire.questionnaire,
        ).exists()

    def test_ready_submission_creates_tracking_record(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire_service: QuestionnaireService,
        eq_org_questionnaire: OrganizationQuestionnaire,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """READY submissions should create EventQuestionnaireSubmission records."""
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=eq_org_questionnaire.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        result = event_questionnaire_service.submit_event_questionnaire(
            user=eq_user,
            event=eq_event,
            questionnaire_service=eq_questionnaire_service,
            org_questionnaire=eq_org_questionnaire,
            submission_schema=submission_schema,
        )

        tracking_record = EventQuestionnaireSubmission.objects.get(
            user=eq_user,
            event=eq_event,
            questionnaire=eq_org_questionnaire.questionnaire,
        )
        assert tracking_record.submission == result
        assert tracking_record.questionnaire_type == eq_org_questionnaire.questionnaire_type


# --- Conditional unique constraint tests ---


class TestConditionalUniqueConstraint:
    """Tests for conditional unique constraint (only FEEDBACK enforces uniqueness)."""

    def test_admission_questionnaire_allows_multiple_submissions_at_model_level(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire: Questionnaire,
        eq_org: Organization,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """ADMISSION questionnaires should allow multiple submissions per user per event (model level)."""
        # Create an admission questionnaire
        org_q = OrganizationQuestionnaire.objects.create(
            organization=eq_org,
            questionnaire=eq_questionnaire,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )
        org_q.events.add(eq_event)

        # Create first submission manually
        first_submission = QuestionnaireSubmission.objects.create(
            questionnaire=eq_questionnaire,
            user=eq_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        EventQuestionnaireSubmission.objects.create(
            user=eq_user,
            event=eq_event,
            questionnaire=eq_questionnaire,
            submission=first_submission,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )

        # Create second submission - should NOT raise IntegrityError
        second_submission = QuestionnaireSubmission.objects.create(
            questionnaire=eq_questionnaire,
            user=eq_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        # This should succeed because ADMISSION type doesn't have unique constraint
        EventQuestionnaireSubmission.objects.create(
            user=eq_user,
            event=eq_event,
            questionnaire=eq_questionnaire,
            submission=second_submission,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )

        # Verify both records exist
        assert (
            EventQuestionnaireSubmission.objects.filter(
                user=eq_user,
                event=eq_event,
                questionnaire=eq_questionnaire,
            ).count()
            == 2
        )

    def test_non_feedback_creates_separate_tracking_records_via_service(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire: Questionnaire,
        eq_org: Organization,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """Non-feedback questionnaires should create separate tracking records for each submission via service."""
        # Create an admission questionnaire
        org_q = OrganizationQuestionnaire.objects.create(
            organization=eq_org,
            questionnaire=eq_questionnaire,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )
        org_q.events.add(eq_event)

        questionnaire_service = QuestionnaireService(eq_questionnaire.id)

        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=org_q.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        # First submission
        result1 = event_questionnaire_service.submit_event_questionnaire(
            user=eq_user,
            event=eq_event,
            questionnaire_service=questionnaire_service,
            org_questionnaire=org_q,
            submission_schema=submission_schema,
        )

        # Second submission
        result2 = event_questionnaire_service.submit_event_questionnaire(
            user=eq_user,
            event=eq_event,
            questionnaire_service=questionnaire_service,
            org_questionnaire=org_q,
            submission_schema=submission_schema,
        )

        # Both should have separate tracking records
        records = EventQuestionnaireSubmission.objects.filter(
            user=eq_user,
            event=eq_event,
            questionnaire=eq_questionnaire,
        )
        assert records.count() == 2
        assert {r.submission_id for r in records} == {result1.id, result2.id}


# --- Race condition protection tests ---


class TestRaceConditionProtection:
    """Tests for race condition protection in concurrent submissions."""

    @patch("events.service.event_questionnaire_service.get_or_create_with_race_protection")
    def test_integrity_error_handled_gracefully(
        self,
        mock_get_or_create: MagicMock,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire_service: QuestionnaireService,
        eq_org_questionnaire: OrganizationQuestionnaire,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """Verify IntegrityError in race condition is handled by returning existing record."""
        # Create an existing tracking record
        existing_submission = QuestionnaireSubmission.objects.create(
            questionnaire=eq_org_questionnaire.questionnaire,
            user=eq_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        existing_record = EventQuestionnaireSubmission.objects.create(
            user=eq_user,
            event=eq_event,
            questionnaire=eq_org_questionnaire.questionnaire,
            submission=existing_submission,
            questionnaire_type=eq_org_questionnaire.questionnaire_type,
        )

        # Mock to return the existing record (simulating race condition recovery)
        mock_get_or_create.return_value = (existing_record, False)

        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=eq_org_questionnaire.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        # Should complete without error
        result = event_questionnaire_service.submit_event_questionnaire(
            user=eq_user,
            event=eq_event,
            questionnaire_service=eq_questionnaire_service,
            org_questionnaire=eq_org_questionnaire,
            submission_schema=submission_schema,
        )

        assert result is not None
        mock_get_or_create.assert_called_once()


# --- Atomic transaction tests ---


class TestAtomicTransaction:
    """Tests for atomic transaction behavior."""

    @patch("questionnaires.service.questionnaire_service.QuestionnaireService.submit")
    def test_no_tracking_record_when_questionnaire_submit_fails(
        self,
        mock_submit: MagicMock,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire_service: QuestionnaireService,
        eq_org_questionnaire: OrganizationQuestionnaire,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """If questionnaire submission fails, no tracking record should be created."""
        mock_submit.side_effect = ValueError("Submission failed")

        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=eq_org_questionnaire.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        with pytest.raises(ValueError, match="Submission failed"):
            event_questionnaire_service.submit_event_questionnaire(
                user=eq_user,
                event=eq_event,
                questionnaire_service=eq_questionnaire_service,
                org_questionnaire=eq_org_questionnaire,
                submission_schema=submission_schema,
            )

        # No tracking record should exist due to atomic rollback
        assert not EventQuestionnaireSubmission.objects.filter(
            user=eq_user,
            event=eq_event,
            questionnaire=eq_org_questionnaire.questionnaire,
        ).exists()
