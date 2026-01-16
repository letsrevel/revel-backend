"""Tests for file upload question functionality in QuestionnaireService.

This module tests:
- build() includes file upload questions in returned schema
- submit() handles file upload answer creation with validation
- create_fu_question() and update_fu_question() CRUD operations
- Validation: MIME types, file count limits, file ownership
"""

from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.models import RevelUser
from conftest import RevelUserFactory
from questionnaires.exceptions import (
    FileLimitExceededError,
    FileOwnershipError,
    FileSizeExceededError,
    InvalidFileMimeTypeError,
    MissingMandatoryAnswerError,
)
from questionnaires.models import (
    FileUploadQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireFile,
    QuestionnaireSection,
    QuestionnaireSubmission,
)
from questionnaires.schema import (
    FileUploadSubmissionSchema,
    FreeTextSubmissionSchema,
    MultipleChoiceSubmissionSchema,
    QuestionnaireSubmissionSchema,
)
from questionnaires.service import QuestionnaireService, get_questionnaire_schema

pytestmark = pytest.mark.django_db


# --- Helper fixtures ---


@pytest.fixture
def user(revel_user_factory: RevelUserFactory) -> RevelUser:
    """Provides a standard user instance for file upload tests."""
    return revel_user_factory()


@pytest.fixture
def another_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    """Provides another user instance for ownership tests."""
    return revel_user_factory()


@pytest.fixture
def questionnaire_file(user: RevelUser) -> QuestionnaireFile:
    """Creates a QuestionnaireFile for testing."""
    uploaded_file = SimpleUploadedFile(
        name="test_file.pdf",
        content=b"test content",
        content_type="application/pdf",
    )
    return QuestionnaireFile.objects.create(
        uploader=user,
        file=uploaded_file,
        original_filename="test_file.pdf",
        file_hash="test_hash_001",
        mime_type="application/pdf",
        file_size=12,
    )


@pytest.fixture
def image_file(user: RevelUser) -> QuestionnaireFile:
    """Creates an image QuestionnaireFile for MIME type testing."""
    uploaded_file = SimpleUploadedFile(
        name="test_image.jpg",
        content=b"fake image content",
        content_type="image/jpeg",
    )
    return QuestionnaireFile.objects.create(
        uploader=user,
        file=uploaded_file,
        original_filename="test_image.jpg",
        file_hash="test_hash_002",
        mime_type="image/jpeg",
        file_size=18,
    )


@pytest.fixture
def file_upload_question(questionnaire: Questionnaire) -> FileUploadQuestion:
    """Creates a FileUploadQuestion for testing."""
    return FileUploadQuestion.objects.create(
        questionnaire=questionnaire,
        question="Please upload your document",
        order=1,
    )


# --- Tests for build() including file upload questions ---


class TestBuildWithFileUploadQuestions:
    """Tests for QuestionnaireService.build() with file upload questions."""

    def test_build_includes_top_level_file_upload_questions(self, questionnaire: Questionnaire) -> None:
        """Test that build() includes top-level file upload questions in schema."""
        # Arrange
        FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload your ID",
            order=1,
            allowed_mime_types=["application/pdf", "image/jpeg"],
            max_file_size=10 * 1024 * 1024,
            max_files=2,
        )

        # Act
        service = QuestionnaireService(questionnaire.id)
        schema = service.build()

        # Assert
        assert len(schema.file_upload_questions) == 1
        fu_q = schema.file_upload_questions[0]
        assert fu_q.question == "Upload your ID"
        assert fu_q.allowed_mime_types == ["application/pdf", "image/jpeg"]
        assert fu_q.max_file_size == 10 * 1024 * 1024
        assert fu_q.max_files == 2

    def test_build_includes_file_upload_questions_in_sections(self, questionnaire: Questionnaire) -> None:
        """Test that build() includes file upload questions within sections."""
        # Arrange
        section = QuestionnaireSection.objects.create(
            questionnaire=questionnaire,
            name="Documents Section",
            order=1,
        )
        FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            section=section,
            question="Upload supporting documents",
            order=1,
        )

        # Act
        service = QuestionnaireService(questionnaire.id)
        schema = service.build()

        # Assert
        assert len(schema.file_upload_questions) == 0  # No top-level FU questions
        assert len(schema.sections) == 1
        assert len(schema.sections[0].file_upload_questions) == 1
        assert schema.sections[0].file_upload_questions[0].question == "Upload supporting documents"

    def test_build_includes_depends_on_option_id_for_conditional_file_upload(
        self, questionnaire: Questionnaire
    ) -> None:
        """Test that build() includes depends_on_option_id for conditional file upload questions."""
        # Arrange
        mc_question = MultipleChoiceQuestion.objects.create(
            questionnaire=questionnaire,
            question="Do you have documents?",
            order=1,
        )
        yes_option = MultipleChoiceOption.objects.create(
            question=mc_question,
            option="Yes",
            is_correct=True,
        )
        FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload your documents",
            order=2,
            depends_on_option=yes_option,
        )

        # Act
        service = QuestionnaireService(questionnaire.id)
        schema = service.build()

        # Assert
        assert len(schema.file_upload_questions) == 1
        assert schema.file_upload_questions[0].depends_on_option_id == yes_option.id

    def test_get_questionnaire_schema_includes_file_upload_questions(self, questionnaire: Questionnaire) -> None:
        """Test that get_questionnaire_schema() includes file upload questions."""
        # Arrange
        FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload document",
            order=1,
        )
        section = QuestionnaireSection.objects.create(
            questionnaire=questionnaire,
            name="Section",
            order=1,
        )
        FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            section=section,
            question="Section upload",
            order=1,
        )

        # Act
        schema = get_questionnaire_schema(questionnaire)

        # Assert
        assert len(schema.fileuploadquestion_questions) == 1  # Top-level only
        assert len(schema.sections) == 1
        assert len(schema.sections[0].fileuploadquestion_questions) == 1


# --- Tests for submit() with file upload answers ---


class TestSubmitWithFileUploadAnswers:
    """Tests for QuestionnaireService.submit() with file upload answers."""

    def test_submit_with_valid_file_upload_answer(
        self,
        user: RevelUser,
        questionnaire: Questionnaire,
        file_upload_question: FileUploadQuestion,
        questionnaire_file: QuestionnaireFile,
    ) -> None:
        """Test successful submission with a valid file upload answer."""
        # Arrange
        service = QuestionnaireService(questionnaire.id)
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=questionnaire.id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            file_upload_answers=[
                FileUploadSubmissionSchema(
                    question_id=file_upload_question.id,
                    file_ids=[questionnaire_file.id],
                )
            ],
        )

        # Act
        submission = service.submit(user, submission_schema)

        # Assert
        assert submission.pk is not None
        assert submission.fileuploadanswer_answers.count() == 1
        fu_answer = submission.fileuploadanswer_answers.first()
        assert fu_answer is not None
        assert fu_answer.question == file_upload_question
        assert fu_answer.files.count() == 1
        assert questionnaire_file in fu_answer.files.all()

    def test_submit_with_multiple_files_per_answer(
        self,
        user: RevelUser,
        questionnaire: Questionnaire,
        questionnaire_file: QuestionnaireFile,
        image_file: QuestionnaireFile,
    ) -> None:
        """Test submission with multiple files for a single question."""
        # Arrange
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload multiple documents",
            max_files=3,
        )
        service = QuestionnaireService(questionnaire.id)
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=questionnaire.id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            file_upload_answers=[
                FileUploadSubmissionSchema(
                    question_id=fu_question.id,
                    file_ids=[questionnaire_file.id, image_file.id],
                )
            ],
        )

        # Act
        submission = service.submit(user, submission_schema)

        # Assert
        fu_answer = submission.fileuploadanswer_answers.first()
        assert fu_answer is not None
        assert fu_answer.files.count() == 2

    def test_submit_fails_with_file_not_owned_by_user(
        self,
        user: RevelUser,
        another_user: RevelUser,
        questionnaire: Questionnaire,
        file_upload_question: FileUploadQuestion,
    ) -> None:
        """Test that submission fails when file belongs to another user.

        Files are scoped per user - you can't use another user's files.
        """
        # Arrange - Create file owned by another_user
        uploaded_file = SimpleUploadedFile(
            name="other_user_file.pdf",
            content=b"other content",
            content_type="application/pdf",
        )
        other_users_file = QuestionnaireFile.objects.create(
            uploader=another_user,
            file=uploaded_file,
            original_filename="other_user_file.pdf",
            file_hash="other_user_hash",
            mime_type="application/pdf",
            file_size=13,
        )

        service = QuestionnaireService(questionnaire.id)
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=questionnaire.id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            file_upload_answers=[
                FileUploadSubmissionSchema(
                    question_id=file_upload_question.id,
                    file_ids=[other_users_file.id],
                )
            ],
        )

        # Act & Assert
        with pytest.raises(FileOwnershipError) as exc_info:
            service.submit(user, submission_schema)
        assert "do not exist or do not belong to you" in str(exc_info.value)

    def test_submit_fails_with_nonexistent_file(
        self,
        user: RevelUser,
        questionnaire: Questionnaire,
        file_upload_question: FileUploadQuestion,
    ) -> None:
        """Test that submission fails when file ID doesn't exist."""
        # Arrange
        service = QuestionnaireService(questionnaire.id)
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=questionnaire.id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            file_upload_answers=[
                FileUploadSubmissionSchema(
                    question_id=file_upload_question.id,
                    file_ids=[uuid4()],  # Non-existent file ID
                )
            ],
        )

        # Act & Assert
        with pytest.raises(FileOwnershipError) as exc_info:
            service.submit(user, submission_schema)
        assert "do not exist or do not belong to you" in str(exc_info.value)

    def test_submit_fails_exceeding_max_files(
        self,
        user: RevelUser,
        questionnaire: Questionnaire,
    ) -> None:
        """Test that submission fails when exceeding max_files limit."""
        # Arrange
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload one document",
            max_files=1,
        )
        files = []
        for i in range(2):
            uploaded_file = SimpleUploadedFile(
                name=f"file{i}.pdf",
                content=f"content{i}".encode(),
                content_type="application/pdf",
            )
            qf = QuestionnaireFile.objects.create(
                uploader=user,
                file=uploaded_file,
                original_filename=f"file{i}.pdf",
                file_hash=f"max_files_hash_{i}",
                mime_type="application/pdf",
                file_size=8,
            )
            files.append(qf)

        service = QuestionnaireService(questionnaire.id)
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=questionnaire.id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            file_upload_answers=[
                FileUploadSubmissionSchema(
                    question_id=fu_question.id,
                    file_ids=[f.id for f in files],  # 2 files but max is 1
                )
            ],
        )

        # Act & Assert
        with pytest.raises(FileLimitExceededError) as exc_info:
            service.submit(user, submission_schema)
        assert "Too many files" in str(exc_info.value)

    def test_submit_fails_with_invalid_mime_type(
        self,
        user: RevelUser,
        questionnaire: Questionnaire,
        questionnaire_file: QuestionnaireFile,  # PDF file
    ) -> None:
        """Test that submission fails when file MIME type is not allowed."""
        # Arrange - Question only accepts images
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload images only",
            allowed_mime_types=["image/jpeg", "image/png"],
        )

        service = QuestionnaireService(questionnaire.id)
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=questionnaire.id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            file_upload_answers=[
                FileUploadSubmissionSchema(
                    question_id=fu_question.id,
                    file_ids=[questionnaire_file.id],  # PDF, not image
                )
            ],
        )

        # Act & Assert
        with pytest.raises(InvalidFileMimeTypeError) as exc_info:
            service.submit(user, submission_schema)
        assert "which is not allowed" in str(exc_info.value)

    def test_submit_accepts_allowed_mime_type(
        self,
        user: RevelUser,
        questionnaire: Questionnaire,
        image_file: QuestionnaireFile,  # JPEG file
    ) -> None:
        """Test that submission succeeds when file MIME type is allowed."""
        # Arrange - Question accepts images
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload images only",
            allowed_mime_types=["image/jpeg", "image/png"],
        )

        service = QuestionnaireService(questionnaire.id)
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=questionnaire.id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            file_upload_answers=[
                FileUploadSubmissionSchema(
                    question_id=fu_question.id,
                    file_ids=[image_file.id],
                )
            ],
        )

        # Act
        submission = service.submit(user, submission_schema)

        # Assert
        assert submission.fileuploadanswer_answers.count() == 1

    def test_submit_accepts_any_mime_type_when_not_restricted(
        self,
        user: RevelUser,
        questionnaire: Questionnaire,
        questionnaire_file: QuestionnaireFile,
    ) -> None:
        """Test that submission accepts any MIME type when allowed_mime_types is empty."""
        # Arrange - No MIME type restriction
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload any file",
            allowed_mime_types=[],  # Empty = allow all
        )

        service = QuestionnaireService(questionnaire.id)
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=questionnaire.id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            file_upload_answers=[
                FileUploadSubmissionSchema(
                    question_id=fu_question.id,
                    file_ids=[questionnaire_file.id],
                )
            ],
        )

        # Act
        submission = service.submit(user, submission_schema)

        # Assert
        assert submission.fileuploadanswer_answers.count() == 1

    def test_submit_fails_with_file_exceeding_max_size(
        self,
        user: RevelUser,
        questionnaire: Questionnaire,
    ) -> None:
        """Test that submission fails when file exceeds max_file_size."""
        # Arrange - Question with 100 byte limit
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload small file",
            max_file_size=100,  # 100 bytes
        )
        # Create a file larger than 100 bytes
        uploaded_file = SimpleUploadedFile(
            name="large_file.pdf",
            content=b"x" * 200,  # 200 bytes
            content_type="application/pdf",
        )
        large_file = QuestionnaireFile.objects.create(
            uploader=user,
            file=uploaded_file,
            original_filename="large_file.pdf",
            file_hash="large_file_hash",
            mime_type="application/pdf",
            file_size=200,
        )

        service = QuestionnaireService(questionnaire.id)
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=questionnaire.id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            file_upload_answers=[
                FileUploadSubmissionSchema(
                    question_id=fu_question.id,
                    file_ids=[large_file.id],
                )
            ],
        )

        # Act & Assert
        with pytest.raises(FileSizeExceededError) as exc_info:
            service.submit(user, submission_schema)
        assert "exceeds maximum size" in str(exc_info.value)

    def test_submit_fails_missing_mandatory_file_upload_question(
        self,
        user: RevelUser,
        questionnaire: Questionnaire,
    ) -> None:
        """Test that submission fails when mandatory file upload question is not answered."""
        # Arrange
        FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Required document upload",
            is_mandatory=True,
        )

        service = QuestionnaireService(questionnaire.id)
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=questionnaire.id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            file_upload_answers=[],  # No answers provided
        )

        # Act & Assert
        with pytest.raises(MissingMandatoryAnswerError):
            service.submit(user, submission_schema)

    def test_submit_draft_allows_missing_mandatory_file_upload(
        self,
        user: RevelUser,
        questionnaire: Questionnaire,
    ) -> None:
        """Test that draft submission allows missing mandatory file upload answers."""
        # Arrange
        FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Required document upload",
            is_mandatory=True,
        )

        service = QuestionnaireService(questionnaire.id)
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=questionnaire.id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT,
            file_upload_answers=[],
        )

        # Act
        submission = service.submit(user, submission_schema)

        # Assert
        assert submission.status == QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT

    def test_submit_with_mixed_answer_types(
        self,
        user: RevelUser,
        questionnaire: Questionnaire,
        questionnaire_file: QuestionnaireFile,
    ) -> None:
        """Test submission with multiple choice, free text, and file upload answers."""
        # Arrange
        mc_question = MultipleChoiceQuestion.objects.create(
            questionnaire=questionnaire,
            question="Choose one",
            order=1,
        )
        mc_option = MultipleChoiceOption.objects.create(
            question=mc_question,
            option="Option A",
            is_correct=True,
        )
        from questionnaires.models import FreeTextQuestion

        ft_question = FreeTextQuestion.objects.create(
            questionnaire=questionnaire,
            question="Explain",
            order=2,
        )
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload",
            order=3,
        )

        service = QuestionnaireService(questionnaire.id)
        submission_schema = QuestionnaireSubmissionSchema(
            questionnaire_id=questionnaire.id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            multiple_choice_answers=[
                MultipleChoiceSubmissionSchema(
                    question_id=mc_question.id,
                    options_id=[mc_option.id],
                )
            ],
            free_text_answers=[
                FreeTextSubmissionSchema(
                    question_id=ft_question.id,
                    answer="My explanation",
                )
            ],
            file_upload_answers=[
                FileUploadSubmissionSchema(
                    question_id=fu_question.id,
                    file_ids=[questionnaire_file.id],
                )
            ],
        )

        # Act
        submission = service.submit(user, submission_schema)

        # Assert
        assert submission.multiplechoiceanswer_answers.count() == 1
        assert submission.freetextanswer_answers.count() == 1
        assert submission.fileuploadanswer_answers.count() == 1
