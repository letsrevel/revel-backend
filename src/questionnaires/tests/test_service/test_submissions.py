"""Tests for QuestionnaireService submission retrieval methods."""

import pytest
from django.http import Http404

from accounts.models import RevelUser
from questionnaires.models import (
    FreeTextAnswer,
    FreeTextQuestion,
    MultipleChoiceAnswer,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSubmission,
)
from questionnaires.service import QuestionnaireService

pytestmark = pytest.mark.django_db


def test_get_submissions_queryset(questionnaire: Questionnaire, user: RevelUser) -> None:
    """Test that submissions queryset is retrieved correctly."""
    # Create some submissions
    submission1 = QuestionnaireSubmission.objects.create(
        user=user, questionnaire=questionnaire, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )
    submission2 = QuestionnaireSubmission.objects.create(
        user=user, questionnaire=questionnaire, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )

    # Create submission for different questionnaire to ensure filtering works
    other_questionnaire = Questionnaire.objects.create(name="Other Questionnaire")
    QuestionnaireSubmission.objects.create(
        user=user, questionnaire=other_questionnaire, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )

    service = QuestionnaireService(questionnaire.id)
    submissions = service.get_submissions_queryset()

    # Should only return submissions for this questionnaire
    submission_ids = list(submissions.values_list("id", flat=True))
    assert len(submission_ids) == 2
    assert submission1.id in submission_ids
    assert submission2.id in submission_ids


def test_get_submission_detail(
    questionnaire: Questionnaire,
    user: RevelUser,
    single_answer_mc_question: MultipleChoiceQuestion,
    correct_option: MultipleChoiceOption,
    free_text_question: FreeTextQuestion,
) -> None:
    """Test that submission detail is retrieved with answers."""
    # Create submission with answers
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)

    # Add answers
    mc_answer = MultipleChoiceAnswer.objects.create(
        submission=submission, question=single_answer_mc_question, option=correct_option
    )
    ft_answer = FreeTextAnswer.objects.create(
        submission=submission, question=free_text_question, answer="This is my answer"
    )

    service = QuestionnaireService(questionnaire.id)
    retrieved_submission = service.get_submission_detail(submission.id)

    assert retrieved_submission.id == submission.id
    assert retrieved_submission.user == user
    assert retrieved_submission.questionnaire == questionnaire

    # Check that related data is prefetched
    mc_answers = list(retrieved_submission.multiplechoiceanswer_answers.all())
    ft_answers = list(retrieved_submission.freetextanswer_answers.all())

    assert len(mc_answers) == 1
    assert len(ft_answers) == 1
    assert mc_answers[0].id == mc_answer.id
    assert ft_answers[0].id == ft_answer.id


def test_get_submission_detail_wrong_questionnaire(questionnaire: Questionnaire, user: RevelUser) -> None:
    """Test that get_submission_detail raises Http404 for wrong questionnaire."""

    # Create submission for different questionnaire
    other_questionnaire = Questionnaire.objects.create(name="Other Questionnaire")
    submission = QuestionnaireSubmission.objects.create(user=user, questionnaire=other_questionnaire)

    service = QuestionnaireService(questionnaire.id)

    with pytest.raises(Http404):
        service.get_submission_detail(submission.id)
