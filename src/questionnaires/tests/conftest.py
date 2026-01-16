"""conftest.py: Fixtures for the questionnaires app."""

import typing as t

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.models import RevelUser
from events.models import Organization, OrganizationQuestionnaire
from questionnaires.llms.llm_backends import MockEvaluator
from questionnaires.models import (
    FileUploadAnswer,
    FileUploadQuestion,
    FreeTextQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireFile,
    QuestionnaireSection,
    QuestionnaireSubmission,
)

User = get_user_model()


@pytest.fixture
def user() -> t.Any:
    """Provides a standard user instance."""
    return User.objects.create_user(username="testuser", password="password")


@pytest.fixture
def organization(user: t.Any) -> t.Any:
    """Provides an Organization instance for questionnaire tests."""

    return Organization.objects.create(name="Test Organization", slug="test-org", owner=user)


@pytest.fixture
def org_questionnaire(organization: Organization, questionnaire: Questionnaire) -> OrganizationQuestionnaire:
    """Link the questionnaire to the organization.

    This is required for the notification system which needs to determine
    which organization a questionnaire belongs to when sending evaluation notifications.
    """

    return OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)


@pytest.fixture
def another_questionnaire() -> Questionnaire:
    """Provides a second, distinct Questionnaire instance."""
    return Questionnaire.objects.create(name="Another Questionnaire")


@pytest.fixture
def draft_submission(user: t.Any, questionnaire: Questionnaire) -> QuestionnaireSubmission:
    """Provides a draft submission for the standard user and questionnaire."""
    return QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)


@pytest.fixture
def submitted_submission(draft_submission: QuestionnaireSubmission) -> QuestionnaireSubmission:
    """Provides a submitted submission, ready for evaluation."""
    draft_submission.status = QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    draft_submission.save()
    return draft_submission


@pytest.fixture
def section(questionnaire: Questionnaire) -> QuestionnaireSection:
    """Provides a section linked to the main questionnaire."""
    return QuestionnaireSection.objects.create(questionnaire=questionnaire, name="Section 1", order=1)


@pytest.fixture
def single_answer_mc_question(questionnaire: Questionnaire) -> MultipleChoiceQuestion:
    """Provides a MultipleChoiceQuestion that allows only one answer."""
    return MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="What is your favorite color?", allow_multiple_answers=False, order=1
    )


@pytest.fixture
def multi_answer_mc_question(questionnaire: Questionnaire) -> MultipleChoiceQuestion:
    """Provides a MultipleChoiceQuestion that allows multiple answers."""
    return MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Which colors do you like?", allow_multiple_answers=True, order=2
    )


@pytest.fixture
def correct_option(single_answer_mc_question: MultipleChoiceQuestion) -> MultipleChoiceOption:
    """Provides a correct option for the single-answer question."""
    return MultipleChoiceOption.objects.create(question=single_answer_mc_question, option="Blue", is_correct=True)


@pytest.fixture
def incorrect_option(single_answer_mc_question: MultipleChoiceQuestion) -> MultipleChoiceOption:
    """Provides an incorrect option for the single-answer question."""
    return MultipleChoiceOption.objects.create(question=single_answer_mc_question, option="Red", is_correct=False)


@pytest.fixture
def free_text_question(questionnaire: Questionnaire) -> FreeTextQuestion:
    """Provides a FreeTextQuestion instance."""
    return FreeTextQuestion.objects.create(
        questionnaire=questionnaire, question="Explain your reasoning.", order=3, llm_guidelines="Be concise."
    )


@pytest.fixture
def mock_evaluator() -> MockEvaluator:
    """Provides an instance of the MockBatchEvaluator."""
    return MockEvaluator()


# --- File Upload Question Fixtures ---


@pytest.fixture
def questionnaire_file(user: RevelUser) -> QuestionnaireFile:
    """Provides a QuestionnaireFile instance owned by the test user."""
    uploaded_file = SimpleUploadedFile(
        name="test_document.pdf",
        content=b"test file content",
        content_type="application/pdf",
    )
    return QuestionnaireFile.objects.create(
        uploader=user,
        file=uploaded_file,
        original_filename="test_document.pdf",
        file_hash="conftest_hash_001",
        mime_type="application/pdf",
        file_size=len(b"test file content"),
    )


@pytest.fixture
def image_questionnaire_file(user: RevelUser) -> QuestionnaireFile:
    """Provides a QuestionnaireFile with image MIME type."""
    content = b"fake image content"
    uploaded_file = SimpleUploadedFile(
        name="test_image.jpg",
        content=content,
        content_type="image/jpeg",
    )
    return QuestionnaireFile.objects.create(
        uploader=user,
        file=uploaded_file,
        original_filename="test_image.jpg",
        file_hash="conftest_hash_002",
        mime_type="image/jpeg",
        file_size=len(content),
    )


@pytest.fixture
def file_upload_question(questionnaire: Questionnaire) -> FileUploadQuestion:
    """Provides a FileUploadQuestion instance for the test questionnaire."""
    return FileUploadQuestion.objects.create(
        questionnaire=questionnaire,
        question="Please upload your document",
        order=1,
    )


@pytest.fixture
def mandatory_file_upload_question(questionnaire: Questionnaire) -> FileUploadQuestion:
    """Provides a mandatory FileUploadQuestion instance."""
    return FileUploadQuestion.objects.create(
        questionnaire=questionnaire,
        question="Required document upload",
        is_mandatory=True,
        order=1,
    )


@pytest.fixture
def image_only_file_upload_question(questionnaire: Questionnaire) -> FileUploadQuestion:
    """Provides a FileUploadQuestion that only accepts image files."""
    return FileUploadQuestion.objects.create(
        questionnaire=questionnaire,
        question="Upload images only",
        allowed_mime_types=["image/jpeg", "image/png", "image/gif"],
        order=1,
    )


@pytest.fixture
def multi_file_upload_question(questionnaire: Questionnaire) -> FileUploadQuestion:
    """Provides a FileUploadQuestion that accepts multiple files."""
    return FileUploadQuestion.objects.create(
        questionnaire=questionnaire,
        question="Upload multiple documents (max 5)",
        max_files=5,
        order=1,
    )


@pytest.fixture
def section_file_upload_question(questionnaire: Questionnaire, section: QuestionnaireSection) -> FileUploadQuestion:
    """Provides a FileUploadQuestion in a section."""
    return FileUploadQuestion.objects.create(
        questionnaire=questionnaire,
        section=section,
        question="Upload documents for this section",
        order=1,
    )


@pytest.fixture
def file_upload_answer(
    draft_submission: QuestionnaireSubmission,
    file_upload_question: FileUploadQuestion,
    questionnaire_file: QuestionnaireFile,
) -> FileUploadAnswer:
    """Provides a FileUploadAnswer with one file attached."""
    answer = FileUploadAnswer.objects.create(
        submission=draft_submission,
        question=file_upload_question,
    )
    answer.files.add(questionnaire_file)
    return answer
