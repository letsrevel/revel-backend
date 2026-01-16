"""Tests for file upload question CRUD operations in QuestionnaireService.

This module tests:
- create_fu_question() to create new file upload questions
- update_fu_question() to update existing file upload questions
- create_questionnaire() with file upload questions
"""

from decimal import Decimal

import pytest

from questionnaires.exceptions import QuestionIntegrityError, SectionIntegrityError
from questionnaires.models import (
    FileUploadQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSection,
)
from questionnaires.schema import (
    FileUploadQuestionCreateSchema,
    FileUploadQuestionUpdateSchema,
    MultipleChoiceOptionCreateSchema,
    MultipleChoiceQuestionCreateSchema,
    QuestionnaireCreateSchema,
    SectionCreateSchema,
)
from questionnaires.service import QuestionnaireService

pytestmark = pytest.mark.django_db


# --- Helper fixtures ---


@pytest.fixture
def file_upload_question(questionnaire: Questionnaire) -> FileUploadQuestion:
    """Creates a FileUploadQuestion for testing."""
    return FileUploadQuestion.objects.create(
        questionnaire=questionnaire,
        question="Please upload your document",
        order=1,
    )


# --- Tests for create_fu_question() ---


class TestCreateFileUploadQuestion:
    """Tests for QuestionnaireService.create_fu_question()."""

    def test_create_fu_question_with_defaults(self, questionnaire: Questionnaire) -> None:
        """Test creating a file upload question with default values."""
        # Arrange
        service = QuestionnaireService(questionnaire.id)
        payload = FileUploadQuestionCreateSchema(
            question="Upload your document",
        )

        # Act
        question = service.create_fu_question(payload)

        # Assert
        assert question.pk is not None
        assert question.question == "Upload your document"
        assert question.questionnaire == questionnaire
        assert question.allowed_mime_types == []
        assert question.max_file_size == 5 * 1024 * 1024  # 5MB default
        assert question.max_files == 1
        assert question.positive_weight == Decimal("0.0")  # Informational default

    def test_create_fu_question_with_custom_settings(self, questionnaire: Questionnaire) -> None:
        """Test creating a file upload question with custom settings."""
        # Arrange
        service = QuestionnaireService(questionnaire.id)
        payload = FileUploadQuestionCreateSchema(
            question="Upload images",
            hint="Only JPEG and PNG allowed",
            allowed_mime_types=["image/jpeg", "image/png"],
            max_file_size=10 * 1024 * 1024,
            max_files=5,
            is_mandatory=True,
            order=2,
        )

        # Act
        question = service.create_fu_question(payload)

        # Assert
        assert question.question == "Upload images"
        assert question.hint == "Only JPEG and PNG allowed"
        assert question.allowed_mime_types == ["image/jpeg", "image/png"]
        assert question.max_file_size == 10 * 1024 * 1024
        assert question.max_files == 5
        assert question.is_mandatory is True
        assert question.order == 2

    def test_create_fu_question_in_section(self, questionnaire: Questionnaire) -> None:
        """Test creating a file upload question within a section."""
        # Arrange
        section = QuestionnaireSection.objects.create(
            questionnaire=questionnaire,
            name="Documents",
            order=1,
        )
        service = QuestionnaireService(questionnaire.id)
        payload = FileUploadQuestionCreateSchema(
            question="Section upload",
            section_id=section.id,
        )

        # Act
        question = service.create_fu_question(payload)

        # Assert
        assert question.section == section
        assert question.questionnaire == questionnaire

    def test_create_fu_question_with_invalid_section_raises_error(
        self, questionnaire: Questionnaire, another_questionnaire: Questionnaire
    ) -> None:
        """Test that creating with a section from another questionnaire fails."""
        # Arrange
        other_section = QuestionnaireSection.objects.create(
            questionnaire=another_questionnaire,
            name="Other Section",
            order=1,
        )
        service = QuestionnaireService(questionnaire.id)
        payload = FileUploadQuestionCreateSchema(
            question="Invalid section",
            section_id=other_section.id,
        )

        # Act & Assert
        with pytest.raises(SectionIntegrityError):
            service.create_fu_question(payload)

    def test_create_fu_question_with_depends_on_option(self, questionnaire: Questionnaire) -> None:
        """Test creating a conditional file upload question."""
        # Arrange
        mc_question = MultipleChoiceQuestion.objects.create(
            questionnaire=questionnaire,
            question="Do you have documents?",
        )
        yes_option = MultipleChoiceOption.objects.create(
            question=mc_question,
            option="Yes",
            is_correct=True,
        )
        service = QuestionnaireService(questionnaire.id)
        payload = FileUploadQuestionCreateSchema(
            question="Upload your documents",
            depends_on_option_id=yes_option.id,
        )

        # Act
        question = service.create_fu_question(payload)

        # Assert
        assert question.depends_on_option == yes_option

    def test_create_fu_question_with_invalid_depends_on_option_raises_error(
        self, questionnaire: Questionnaire, another_questionnaire: Questionnaire
    ) -> None:
        """Test that depends_on_option from another questionnaire fails."""
        # Arrange
        other_mc_question = MultipleChoiceQuestion.objects.create(
            questionnaire=another_questionnaire,
            question="Other question",
        )
        other_option = MultipleChoiceOption.objects.create(
            question=other_mc_question,
            option="Other option",
        )
        service = QuestionnaireService(questionnaire.id)
        payload = FileUploadQuestionCreateSchema(
            question="Invalid dependency",
            depends_on_option_id=other_option.id,
        )

        # Act & Assert
        with pytest.raises(QuestionIntegrityError):
            service.create_fu_question(payload)


# --- Tests for update_fu_question() ---


class TestUpdateFileUploadQuestion:
    """Tests for QuestionnaireService.update_fu_question()."""

    def test_update_fu_question_basic(
        self,
        questionnaire: Questionnaire,
        file_upload_question: FileUploadQuestion,
    ) -> None:
        """Test updating basic file upload question properties."""
        # Arrange
        service = QuestionnaireService(questionnaire.id)
        payload = FileUploadQuestionUpdateSchema(
            question="Updated question text",
            hint="New hint",
            is_mandatory=True,
        )

        # Act
        updated = service.update_fu_question(file_upload_question, payload)

        # Assert
        assert updated.question == "Updated question text"
        assert updated.hint == "New hint"
        assert updated.is_mandatory is True

    def test_update_fu_question_mime_types(
        self,
        questionnaire: Questionnaire,
        file_upload_question: FileUploadQuestion,
    ) -> None:
        """Test updating allowed_mime_types."""
        # Arrange
        service = QuestionnaireService(questionnaire.id)
        payload = FileUploadQuestionUpdateSchema(
            question=file_upload_question.question or "",
            allowed_mime_types=["image/jpeg", "image/png", "image/gif"],
        )

        # Act
        updated = service.update_fu_question(file_upload_question, payload)

        # Assert
        assert updated.allowed_mime_types == ["image/jpeg", "image/png", "image/gif"]

    def test_update_fu_question_max_files(
        self,
        questionnaire: Questionnaire,
        file_upload_question: FileUploadQuestion,
    ) -> None:
        """Test updating max_files limit."""
        # Arrange
        service = QuestionnaireService(questionnaire.id)
        payload = FileUploadQuestionUpdateSchema(
            question=file_upload_question.question or "",
            max_files=10,
        )

        # Act
        updated = service.update_fu_question(file_upload_question, payload)

        # Assert
        assert updated.max_files == 10

    def test_update_fu_question_move_to_section(
        self, questionnaire: Questionnaire, file_upload_question: FileUploadQuestion
    ) -> None:
        """Test moving a file upload question to a section."""
        # Arrange
        section = QuestionnaireSection.objects.create(
            questionnaire=questionnaire,
            name="New Section",
            order=1,
        )
        service = QuestionnaireService(questionnaire.id)
        payload = FileUploadQuestionUpdateSchema(
            question=file_upload_question.question or "",
            section_id=section.id,
        )

        # Act
        updated = service.update_fu_question(file_upload_question, payload)

        # Assert
        assert updated.section == section

    def test_update_fu_question_invalid_section_raises_error(
        self,
        questionnaire: Questionnaire,
        file_upload_question: FileUploadQuestion,
        another_questionnaire: Questionnaire,
    ) -> None:
        """Test that moving to a section from another questionnaire fails."""
        # Arrange
        other_section = QuestionnaireSection.objects.create(
            questionnaire=another_questionnaire,
            name="Other Section",
            order=1,
        )
        service = QuestionnaireService(questionnaire.id)
        payload = FileUploadQuestionUpdateSchema(
            question=file_upload_question.question or "",
            section_id=other_section.id,
        )

        # Act & Assert
        with pytest.raises(SectionIntegrityError):
            service.update_fu_question(file_upload_question, payload)


# --- Tests for create_questionnaire with file upload questions ---


class TestCreateQuestionnaireWithFileUploadQuestions:
    """Tests for QuestionnaireService.create_questionnaire() with file upload questions."""

    def test_create_questionnaire_with_top_level_file_upload_question(self) -> None:
        """Test creating a questionnaire with top-level file upload questions."""
        # Arrange
        payload = QuestionnaireCreateSchema(
            name="Questionnaire with File Upload",
            min_score=Decimal(0),
            evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL,
            fileuploadquestion_questions=[
                FileUploadQuestionCreateSchema(
                    question="Upload your ID",
                    allowed_mime_types=["application/pdf", "image/jpeg"],
                    max_files=2,
                )
            ],
        )

        # Act
        questionnaire = QuestionnaireService.create_questionnaire(payload)

        # Assert
        assert questionnaire.fileuploadquestion_questions.count() == 1
        fu_q = questionnaire.fileuploadquestion_questions.first()
        assert fu_q is not None
        assert fu_q.question == "Upload your ID"
        assert fu_q.allowed_mime_types == ["application/pdf", "image/jpeg"]
        assert fu_q.max_files == 2

    def test_create_questionnaire_with_file_upload_in_section(self) -> None:
        """Test creating a questionnaire with file upload questions in sections."""
        # Arrange
        payload = QuestionnaireCreateSchema(
            name="Questionnaire with Section FU",
            min_score=Decimal(0),
            evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL,
            sections=[
                SectionCreateSchema(
                    name="Document Section",
                    order=1,
                    fileuploadquestion_questions=[
                        FileUploadQuestionCreateSchema(
                            question="Upload supporting documents",
                        )
                    ],
                )
            ],
        )

        # Act
        questionnaire = QuestionnaireService.create_questionnaire(payload)

        # Assert
        assert questionnaire.sections.count() == 1
        section = questionnaire.sections.first()
        assert section is not None
        assert section.fileuploadquestion_questions.count() == 1
        fu_q = section.fileuploadquestion_questions.first()
        assert fu_q is not None
        assert fu_q.question == "Upload supporting documents"

    def test_create_questionnaire_with_conditional_file_upload(self) -> None:
        """Test creating a questionnaire with conditional file upload question via option."""
        # Arrange
        payload = QuestionnaireCreateSchema(
            name="Conditional FU Questionnaire",
            min_score=Decimal(0),
            evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL,
            multiplechoicequestion_questions=[
                MultipleChoiceQuestionCreateSchema(
                    question="Do you have supporting documents?",
                    order=1,
                    options=[
                        MultipleChoiceOptionCreateSchema(
                            option="Yes",
                            is_correct=True,
                            conditional_fu_questions=[
                                FileUploadQuestionCreateSchema(
                                    question="Upload your documents",
                                    is_mandatory=True,
                                )
                            ],
                        ),
                        MultipleChoiceOptionCreateSchema(
                            option="No",
                            is_correct=False,
                        ),
                    ],
                )
            ],
        )

        # Act
        questionnaire = QuestionnaireService.create_questionnaire(payload)

        # Assert
        mc_q = questionnaire.multiplechoicequestion_questions.first()
        assert mc_q is not None
        yes_option = mc_q.options.get(option="Yes")
        fu_questions = questionnaire.fileuploadquestion_questions.filter(depends_on_option=yes_option)
        assert fu_questions.count() == 1
        assert fu_questions.first().question == "Upload your documents"  # type: ignore[union-attr]
