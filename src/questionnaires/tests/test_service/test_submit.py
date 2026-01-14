"""Tests for the QuestionnaireService.submit() method."""

import pytest

from accounts.models import RevelUser
from questionnaires.exceptions import (
    CrossQuestionnaireSubmissionError,
    MissingMandatoryAnswerError,
)
from questionnaires.models import (
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSubmission,
)
from questionnaires.schema import (
    FreeTextSubmissionSchema,
    MultipleChoiceSubmissionSchema,
    QuestionnaireSubmissionSchema,
)
from questionnaires.service import QuestionnaireService

pytestmark = pytest.mark.django_db


def test_submit_success_final(user: RevelUser, complex_questionnaire: Questionnaire) -> None:
    """Test a successful, final submission of a questionnaire."""
    q = complex_questionnaire
    service = QuestionnaireService(q.id)

    # Get question and option IDs for all mandatory questions
    mcq_top = q.multiplechoicequestion_questions.get(section__isnull=True, is_mandatory=True)
    mcq_s1 = q.multiplechoicequestion_questions.get(section__name="Section 1", is_mandatory=True)
    ftq_s2 = q.freetextquestion_questions.get(section__name="Section 2", is_mandatory=True)
    mcq_top_opt = mcq_top.options.get(is_correct=True)
    mcq_s1_opt = mcq_s1.options.get(is_correct=True)

    submission_schema = QuestionnaireSubmissionSchema(
        questionnaire_id=q.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=mcq_top.id, options_id=[mcq_top_opt.id]),
            MultipleChoiceSubmissionSchema(question_id=mcq_s1.id, options_id=[mcq_s1_opt.id]),
        ],
        free_text_answers=[FreeTextSubmissionSchema(question_id=ftq_s2.id, answer="This is a mandatory answer.")],
    )

    submission = service.submit(user, submission_schema)

    assert submission.pk is not None
    assert submission.user == user
    assert submission.questionnaire == q
    assert submission.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    assert submission.submitted_at is not None
    assert submission.multiplechoiceanswer_answers.count() == 2
    assert submission.freetextanswer_answers.count() == 1


def test_submit_draft_and_update(user: RevelUser, complex_questionnaire: Questionnaire) -> None:
    """Test creating a draft and then updating it by adding more answers."""
    q = complex_questionnaire
    service = QuestionnaireService(q.id)
    mcq_top = q.multiplechoicequestion_questions.get(section__isnull=True)
    mcq_top_opt = mcq_top.options.get(is_correct=True)

    # First submission as draft
    draft_schema_1 = QuestionnaireSubmissionSchema(
        questionnaire_id=q.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=mcq_top.id, options_id=[mcq_top_opt.id]),
        ],
    )
    submission1 = service.submit(user, draft_schema_1)

    assert submission1.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT
    assert QuestionnaireSubmission.objects.count() == 1
    assert submission1.multiplechoiceanswer_answers.count() == 1

    # Second submission as draft. The current implementation APPENDS answers.
    ftq_s2 = q.freetextquestion_questions.get(section__name="Section 2")
    draft_schema_2 = QuestionnaireSubmissionSchema(
        questionnaire_id=q.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT,
        free_text_answers=[FreeTextSubmissionSchema(question_id=ftq_s2.id, answer="An appended answer.")],
    )
    submission2 = service.submit(user, draft_schema_2)

    assert submission2.id == submission1.id
    assert QuestionnaireSubmission.objects.count() == 1
    # Check that answers were replaced.
    assert submission2.multiplechoiceanswer_answers.count() == 0
    assert submission2.freetextanswer_answers.count() == 1


def test_submit_raises_missing_mandatory_error(user: RevelUser, complex_questionnaire: Questionnaire) -> None:
    """Test that submitting without all mandatory answers raises a MissingMandatoryAnswerError."""
    q = complex_questionnaire
    service = QuestionnaireService(q.id)

    # Only answer one of the three mandatory questions
    mcq_top = q.multiplechoicequestion_questions.get(section__isnull=True, is_mandatory=True)
    mcq_top_opt = mcq_top.options.get(is_correct=True)

    submission_schema = QuestionnaireSubmissionSchema(
        questionnaire_id=q.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=mcq_top.id, options_id=[mcq_top_opt.id]),
        ],
    )

    with pytest.raises(MissingMandatoryAnswerError):
        service.submit(user, submission_schema)

    # Verify transactionality: no submission or answers should have been created.
    assert QuestionnaireSubmission.objects.count() == 0


def test_submit_raises_cross_questionnaire_error(
    user: RevelUser, complex_questionnaire: Questionnaire, another_questionnaire: Questionnaire
) -> None:
    """Test that submitting an answer for a different questionnaire raises an error."""
    service = QuestionnaireService(complex_questionnaire.id)

    # Create a question in the *other* questionnaire
    other_mcq = MultipleChoiceQuestion.objects.create(questionnaire=another_questionnaire, question="Wrong Q")
    other_opt = MultipleChoiceOption.objects.create(question=other_mcq, option="Wrong Opt", is_correct=True)

    submission_schema = QuestionnaireSubmissionSchema(
        questionnaire_id=complex_questionnaire.id,
        status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        multiple_choice_answers=[
            MultipleChoiceSubmissionSchema(question_id=other_mcq.id, options_id=[other_opt.id]),
        ],
    )

    with pytest.raises(CrossQuestionnaireSubmissionError):
        service.submit(user, submission_schema)

    assert QuestionnaireSubmission.objects.count() == 0
