"""Tests for admission questionnaire resubmission validation.

This validates the same logic as QuestionnaireGate but at submission time,
covering: pending evaluation, pending review, approved, rejected with retake
eligibility (max_attempts, cooldown, immediate retake), draft bypass, and
membership questionnaire exemption.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, EventQuestionnaireSubmission, Organization, OrganizationQuestionnaire
from events.service import event_questionnaire_service
from questionnaires.models import (
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireSubmission,
)
from questionnaires.schema import MultipleChoiceSubmissionSchema, QuestionnaireSubmissionSchema
from questionnaires.service.questionnaire_service import QuestionnaireService

pytestmark = pytest.mark.django_db


# --- Fixtures (eq_* fixtures are in conftest.py) ---


@pytest.fixture
def admission_org_questionnaire(
    eq_org: Organization,
    eq_questionnaire: Questionnaire,
    eq_event: Event,
) -> OrganizationQuestionnaire:
    """Admission questionnaire linked to organization and event."""
    org_q = OrganizationQuestionnaire.objects.create(
        organization=eq_org,
        questionnaire=eq_questionnaire,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
    )
    org_q.events.add(eq_event)
    return org_q


class TestAdmissionResubmissionValidation:
    """Tests for admission questionnaire resubmission validation."""

    def test_first_submission_allowed(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire_service: QuestionnaireService,
        admission_org_questionnaire: OrganizationQuestionnaire,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """First admission submission should always be allowed."""
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=admission_org_questionnaire.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        result = event_questionnaire_service.submit_event_questionnaire(
            user=eq_user,
            event=eq_event,
            questionnaire_service=eq_questionnaire_service,
            org_questionnaire=admission_org_questionnaire,
            submission_schema=submission_schema,
        )

        assert result is not None
        assert EventQuestionnaireSubmission.objects.filter(
            user=eq_user,
            event=eq_event,
            questionnaire=admission_org_questionnaire.questionnaire,
        ).exists()

    def test_blocks_when_pending_evaluation(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire_service: QuestionnaireService,
        admission_org_questionnaire: OrganizationQuestionnaire,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """Should block resubmission when previous submission has no evaluation yet."""
        first_submission = QuestionnaireSubmission.objects.create(
            questionnaire=admission_org_questionnaire.questionnaire,
            user=eq_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            submitted_at=timezone.now(),
        )
        EventQuestionnaireSubmission.objects.create(
            user=eq_user,
            event=eq_event,
            questionnaire=admission_org_questionnaire.questionnaire,
            submission=first_submission,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )

        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=admission_org_questionnaire.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        with pytest.raises(HttpError) as exc_info:
            event_questionnaire_service.submit_event_questionnaire(
                user=eq_user,
                event=eq_event,
                questionnaire_service=eq_questionnaire_service,
                org_questionnaire=admission_org_questionnaire,
                submission_schema=submission_schema,
            )

        assert exc_info.value.status_code == 400
        assert "pending evaluation" in str(exc_info.value.message)

    def test_blocks_when_pending_review(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire_service: QuestionnaireService,
        admission_org_questionnaire: OrganizationQuestionnaire,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """Should block resubmission when previous submission is PENDING_REVIEW."""
        first_submission = QuestionnaireSubmission.objects.create(
            questionnaire=admission_org_questionnaire.questionnaire,
            user=eq_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            submitted_at=timezone.now(),
        )
        EventQuestionnaireSubmission.objects.create(
            user=eq_user,
            event=eq_event,
            questionnaire=admission_org_questionnaire.questionnaire,
            submission=first_submission,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )
        QuestionnaireEvaluation.objects.create(
            submission=first_submission,
            status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW,
        )

        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=admission_org_questionnaire.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        with pytest.raises(HttpError) as exc_info:
            event_questionnaire_service.submit_event_questionnaire(
                user=eq_user,
                event=eq_event,
                questionnaire_service=eq_questionnaire_service,
                org_questionnaire=admission_org_questionnaire,
                submission_schema=submission_schema,
            )

        assert exc_info.value.status_code == 400
        assert "pending evaluation" in str(exc_info.value.message)

    def test_blocks_when_approved(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire_service: QuestionnaireService,
        admission_org_questionnaire: OrganizationQuestionnaire,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """Should block resubmission when previous submission is APPROVED."""
        first_submission = QuestionnaireSubmission.objects.create(
            questionnaire=admission_org_questionnaire.questionnaire,
            user=eq_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            submitted_at=timezone.now(),
        )
        EventQuestionnaireSubmission.objects.create(
            user=eq_user,
            event=eq_event,
            questionnaire=admission_org_questionnaire.questionnaire,
            submission=first_submission,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )
        QuestionnaireEvaluation.objects.create(
            submission=first_submission,
            status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
        )

        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=admission_org_questionnaire.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        with pytest.raises(HttpError) as exc_info:
            event_questionnaire_service.submit_event_questionnaire(
                user=eq_user,
                event=eq_event,
                questionnaire_service=eq_questionnaire_service,
                org_questionnaire=admission_org_questionnaire,
                submission_schema=submission_schema,
            )

        assert exc_info.value.status_code == 400
        assert "already been approved" in str(exc_info.value.message)

    def test_blocks_when_max_attempts_reached(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire: Questionnaire,
        eq_questionnaire_service: QuestionnaireService,
        admission_org_questionnaire: OrganizationQuestionnaire,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """Should block resubmission when max_attempts is reached."""
        eq_questionnaire.max_attempts = 1
        eq_questionnaire.can_retake_after = timedelta(seconds=0)
        eq_questionnaire.save()

        first_submission = QuestionnaireSubmission.objects.create(
            questionnaire=admission_org_questionnaire.questionnaire,
            user=eq_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            submitted_at=timezone.now(),
        )
        EventQuestionnaireSubmission.objects.create(
            user=eq_user,
            event=eq_event,
            questionnaire=admission_org_questionnaire.questionnaire,
            submission=first_submission,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )
        QuestionnaireEvaluation.objects.create(
            submission=first_submission,
            status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
        )

        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=admission_org_questionnaire.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        with pytest.raises(HttpError) as exc_info:
            event_questionnaire_service.submit_event_questionnaire(
                user=eq_user,
                event=eq_event,
                questionnaire_service=eq_questionnaire_service,
                org_questionnaire=admission_org_questionnaire,
                submission_schema=submission_schema,
            )

        assert exc_info.value.status_code == 400
        assert "maximum number of attempts" in str(exc_info.value.message)

    def test_blocks_when_cooldown_not_elapsed(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire: Questionnaire,
        eq_questionnaire_service: QuestionnaireService,
        admission_org_questionnaire: OrganizationQuestionnaire,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """Should block resubmission when can_retake_after cooldown hasn't elapsed."""
        eq_questionnaire.can_retake_after = timedelta(hours=1)
        eq_questionnaire.max_attempts = 0  # Unlimited
        eq_questionnaire.save()

        first_submission = QuestionnaireSubmission.objects.create(
            questionnaire=admission_org_questionnaire.questionnaire,
            user=eq_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            submitted_at=timezone.now(),
        )
        EventQuestionnaireSubmission.objects.create(
            user=eq_user,
            event=eq_event,
            questionnaire=admission_org_questionnaire.questionnaire,
            submission=first_submission,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )
        QuestionnaireEvaluation.objects.create(
            submission=first_submission,
            status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
        )

        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=admission_org_questionnaire.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        with pytest.raises(HttpError) as exc_info:
            event_questionnaire_service.submit_event_questionnaire(
                user=eq_user,
                event=eq_event,
                questionnaire_service=eq_questionnaire_service,
                org_questionnaire=admission_org_questionnaire,
                submission_schema=submission_schema,
            )

        assert exc_info.value.status_code == 400
        assert "retry after" in str(exc_info.value.message)

    def test_allows_immediate_retake_when_can_retake_after_is_none(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire: Questionnaire,
        eq_questionnaire_service: QuestionnaireService,
        admission_org_questionnaire: OrganizationQuestionnaire,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """Should allow immediate retake when can_retake_after is None (no cooldown)."""
        eq_questionnaire.can_retake_after = None
        eq_questionnaire.max_attempts = 0  # Unlimited
        eq_questionnaire.save()

        first_submission = QuestionnaireSubmission.objects.create(
            questionnaire=admission_org_questionnaire.questionnaire,
            user=eq_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            submitted_at=timezone.now(),
        )
        EventQuestionnaireSubmission.objects.create(
            user=eq_user,
            event=eq_event,
            questionnaire=admission_org_questionnaire.questionnaire,
            submission=first_submission,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )
        QuestionnaireEvaluation.objects.create(
            submission=first_submission,
            status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
        )

        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=admission_org_questionnaire.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        result = event_questionnaire_service.submit_event_questionnaire(
            user=eq_user,
            event=eq_event,
            questionnaire_service=eq_questionnaire_service,
            org_questionnaire=admission_org_questionnaire,
            submission_schema=submission_schema,
        )

        assert result is not None
        assert (
            EventQuestionnaireSubmission.objects.filter(
                user=eq_user,
                event=eq_event,
                questionnaire=admission_org_questionnaire.questionnaire,
            ).count()
            == 2
        )

    def test_allows_immediate_retake_when_can_retake_after_is_zero(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire: Questionnaire,
        eq_questionnaire_service: QuestionnaireService,
        admission_org_questionnaire: OrganizationQuestionnaire,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """Should allow immediate retake when can_retake_after is zero duration."""
        eq_questionnaire.can_retake_after = timedelta(0)
        eq_questionnaire.max_attempts = 0  # Unlimited
        eq_questionnaire.save()

        first_submission = QuestionnaireSubmission.objects.create(
            questionnaire=admission_org_questionnaire.questionnaire,
            user=eq_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            submitted_at=timezone.now(),
        )
        EventQuestionnaireSubmission.objects.create(
            user=eq_user,
            event=eq_event,
            questionnaire=admission_org_questionnaire.questionnaire,
            submission=first_submission,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )
        QuestionnaireEvaluation.objects.create(
            submission=first_submission,
            status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
        )

        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=admission_org_questionnaire.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        result = event_questionnaire_service.submit_event_questionnaire(
            user=eq_user,
            event=eq_event,
            questionnaire_service=eq_questionnaire_service,
            org_questionnaire=admission_org_questionnaire,
            submission_schema=submission_schema,
        )

        assert result is not None
        assert (
            EventQuestionnaireSubmission.objects.filter(
                user=eq_user,
                event=eq_event,
                questionnaire=admission_org_questionnaire.questionnaire,
            ).count()
            == 2
        )

    def test_allows_retake_when_rejected_and_eligible(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire: Questionnaire,
        eq_questionnaire_service: QuestionnaireService,
        admission_org_questionnaire: OrganizationQuestionnaire,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """Should allow resubmission when rejected, cooldown elapsed, and attempts remaining."""
        eq_questionnaire.can_retake_after = timedelta(seconds=0)
        eq_questionnaire.max_attempts = 0
        eq_questionnaire.save()

        first_submission = QuestionnaireSubmission.objects.create(
            questionnaire=admission_org_questionnaire.questionnaire,
            user=eq_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            submitted_at=timezone.now() - timedelta(hours=1),
        )
        EventQuestionnaireSubmission.objects.create(
            user=eq_user,
            event=eq_event,
            questionnaire=admission_org_questionnaire.questionnaire,
            submission=first_submission,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )
        QuestionnaireEvaluation.objects.create(
            submission=first_submission,
            status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
        )

        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=admission_org_questionnaire.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        result = event_questionnaire_service.submit_event_questionnaire(
            user=eq_user,
            event=eq_event,
            questionnaire_service=eq_questionnaire_service,
            org_questionnaire=admission_org_questionnaire,
            submission_schema=submission_schema,
        )

        assert result is not None
        assert (
            EventQuestionnaireSubmission.objects.filter(
                user=eq_user,
                event=eq_event,
                questionnaire=admission_org_questionnaire.questionnaire,
            ).count()
            == 2
        )

    def test_draft_submissions_bypass_validation(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire_service: QuestionnaireService,
        admission_org_questionnaire: OrganizationQuestionnaire,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """DRAFT submissions should bypass resubmission validation."""
        first_submission = QuestionnaireSubmission.objects.create(
            questionnaire=admission_org_questionnaire.questionnaire,
            user=eq_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            submitted_at=timezone.now(),
        )
        EventQuestionnaireSubmission.objects.create(
            user=eq_user,
            event=eq_event,
            questionnaire=admission_org_questionnaire.questionnaire,
            submission=first_submission,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )

        draft_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=admission_org_questionnaire.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        result = event_questionnaire_service.submit_event_questionnaire(
            user=eq_user,
            event=eq_event,
            questionnaire_service=eq_questionnaire_service,
            org_questionnaire=admission_org_questionnaire,
            submission_schema=draft_schema,
        )

        assert result is not None
        assert result.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT

    def test_membership_questionnaires_not_affected(
        self,
        eq_user: RevelUser,
        eq_event: Event,
        eq_questionnaire: Questionnaire,
        eq_org: Organization,
        eq_questionnaire_service: QuestionnaireService,
        eq_mcq: MultipleChoiceQuestion,
        eq_option: MultipleChoiceOption,
    ) -> None:
        """MEMBERSHIP questionnaires should not have resubmission validation."""
        org_q = OrganizationQuestionnaire.objects.create(
            organization=eq_org,
            questionnaire=eq_questionnaire,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
        )
        org_q.events.add(eq_event)

        first_submission = QuestionnaireSubmission.objects.create(
            questionnaire=org_q.questionnaire,
            user=eq_user,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            submitted_at=timezone.now(),
        )
        EventQuestionnaireSubmission.objects.create(
            user=eq_user,
            event=eq_event,
            questionnaire=org_q.questionnaire,
            submission=first_submission,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
        )

        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=org_q.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[MultipleChoiceSubmissionSchema(question_id=eq_mcq.id, options_id=[eq_option.id])],
        )

        result = event_questionnaire_service.submit_event_questionnaire(
            user=eq_user,
            event=eq_event,
            questionnaire_service=eq_questionnaire_service,
            org_questionnaire=org_q,
            submission_schema=submission_schema,
        )

        assert result is not None
        assert (
            EventQuestionnaireSubmission.objects.filter(
                user=eq_user,
                event=eq_event,
                questionnaire=org_q.questionnaire,
            ).count()
            == 2
        )
