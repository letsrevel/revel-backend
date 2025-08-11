"""test_models.py: Unit tests for the questionnaire models."""

import pytest
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.utils import timezone

from questionnaires import exceptions
from questionnaires.models import (
    MultipleChoiceAnswer,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSection,
    QuestionnaireSubmission,
)


@pytest.mark.django_db
def test_questionnaire_creation_and_manager(questionnaire: Questionnaire) -> None:
    """Test that a Questionnaire can be created successfully."""
    assert questionnaire.pk is not None
    assert questionnaire.name == "Test Questionnaire"
    assert Questionnaire.objects.with_questions().count() == 1


@pytest.mark.django_db
def test_submission_status_change_sets_submitted_at(draft_submission: QuestionnaireSubmission) -> None:
    """Test that moving a submission to SUBMITTED sets the `submitted_at` timestamp."""
    assert draft_submission.submitted_at is None
    draft_submission.status = QuestionnaireSubmission.Status.READY
    draft_submission.save()  # this will set submitted_at
    draft_submission.refresh_from_db()
    assert draft_submission.submitted_at is not None
    assert isinstance(draft_submission.submitted_at, timezone.datetime)  # type: ignore[unreachable]


@pytest.mark.django_db
def test_unique_draft_submission_constraint(user: AbstractUser, questionnaire: Questionnaire) -> None:
    """Test that a user can only have one draft submission per questionnaire."""
    QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire, status="draft")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        QuestionnaireSubmission(user=user, questionnaire=questionnaire, status="draft").full_clean()  # type: ignore[misc]


@pytest.mark.django_db
def test_multiple_submitted_submissions_allowed(user: AbstractUser, questionnaire: Questionnaire) -> None:
    """Test that a user CAN have multiple submitted submissions."""
    QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire, status="ready")  # type: ignore[misc]
    second_submission = QuestionnaireSubmission(user=user, questionnaire=questionnaire, status="ready")  # type: ignore[misc]
    second_submission.full_clean()
    second_submission.save()
    assert QuestionnaireSubmission.objects.count() == 2


@pytest.mark.django_db
def test_section_cannot_belong_to_different_questionnaire(
    section: QuestionnaireSection, another_questionnaire: Questionnaire
) -> None:
    """Test the BaseQuestion clean method for cross-questionnaire section assignment."""
    with pytest.raises(exceptions.CrossQuestionnaireSectionError):
        question = MultipleChoiceQuestion(questionnaire=another_questionnaire, section=section, question="Q?")
        question.clean()


@pytest.mark.django_db
def test_section_can_belong_to_correct_questionnaire(
    questionnaire: Questionnaire, section: QuestionnaireSection
) -> None:
    """Test that a question can be validly assigned to a section from the same questionnaire."""
    question = MultipleChoiceQuestion(questionnaire=questionnaire, section=section, question="Q?")
    question.clean()
    assert question.section == section


@pytest.mark.django_db
def test_single_answer_question_disallows_multiple_correct_options(
    single_answer_mc_question: MultipleChoiceQuestion, correct_option: MultipleChoiceOption
) -> None:
    """Test that a second correct option cannot be created for a single-answer question."""
    with pytest.raises(exceptions.MultipleCorrectOptionsError):
        another_correct_option = MultipleChoiceOption(
            question=single_answer_mc_question, option="Green", is_correct=True
        )
        another_correct_option.clean()


@pytest.mark.django_db
def test_multi_answer_question_allows_multiple_correct_options(
    multi_answer_mc_question: MultipleChoiceQuestion,
) -> None:
    """Test that a multi-answer question CAN have multiple correct options."""
    MultipleChoiceOption.objects.create(question=multi_answer_mc_question, option="Green", is_correct=True)
    second_correct_option = MultipleChoiceOption(question=multi_answer_mc_question, option="Blue", is_correct=True)
    second_correct_option.full_clean()
    second_correct_option.save()
    assert multi_answer_mc_question.options.filter(is_correct=True).count() == 2


@pytest.mark.django_db
def test_disallowed_multiple_answers_for_single_answer_question(
    draft_submission: QuestionnaireSubmission,
    single_answer_mc_question: MultipleChoiceQuestion,
    correct_option: MultipleChoiceOption,
    incorrect_option: MultipleChoiceOption,
) -> None:
    """Test that a user cannot submit two answers for a single-answer question."""
    MultipleChoiceAnswer.objects.create(
        submission=draft_submission, question=single_answer_mc_question, option=correct_option
    )
    with pytest.raises(exceptions.DisallowedMultipleAnswersError):
        second_answer = MultipleChoiceAnswer(
            submission=draft_submission, question=single_answer_mc_question, option=incorrect_option
        )
        second_answer.clean()


@pytest.mark.django_db
def test_can_update_existing_answer_for_single_answer_question(
    draft_submission: QuestionnaireSubmission,
    single_answer_mc_question: MultipleChoiceQuestion,
    correct_option: MultipleChoiceOption,
    incorrect_option: MultipleChoiceOption,
) -> None:
    """
    Test that updating an existing answer for a single-answer question does not
    trigger a DisallowedMultipleAnswersError.
    """
    # 1. Create the initial answer
    initial_answer = MultipleChoiceAnswer.objects.create(
        submission=draft_submission,
        question=single_answer_mc_question,
        option=incorrect_option,  # User initially chose the wrong option
    )
    assert initial_answer.option == incorrect_option

    # 2. Now, the user changes their mind and selects the correct option.
    # We are updating the existing answer instance.
    initial_answer.option = correct_option

    initial_answer.full_clean()
    initial_answer.save()

    # 3. Verify the update was successful.
    initial_answer.refresh_from_db()
    assert initial_answer.option == correct_option
    assert (
        MultipleChoiceAnswer.objects.filter(submission=draft_submission, question=single_answer_mc_question).count()
        == 1
    )


@pytest.mark.django_db
def test_unique_answer_option_constraint(
    draft_submission: QuestionnaireSubmission,
    single_answer_mc_question: MultipleChoiceQuestion,
    correct_option: MultipleChoiceOption,
) -> None:
    """Test that a user cannot select the same option twice for the same question/submission."""
    MultipleChoiceAnswer.objects.create(
        submission=draft_submission, question=single_answer_mc_question, option=correct_option
    )
    with pytest.raises(ValidationError):
        duplicate_answer = MultipleChoiceAnswer(
            submission=draft_submission, question=single_answer_mc_question, option=correct_option
        )
        duplicate_answer.clean()
