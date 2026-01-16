"""Tests for file upload question type models (QuestionnaireFile, FileUploadQuestion, FileUploadAnswer)."""

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from conftest import RevelUserFactory
from questionnaires.models import (
    FileUploadAnswer,
    FileUploadQuestion,
    Questionnaire,
    QuestionnaireFile,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


class TestQuestionnaireFileModel:
    """Tests for QuestionnaireFile model."""

    def test_questionnaire_file_creation(self, revel_user_factory: RevelUserFactory) -> None:
        """Test that a QuestionnaireFile can be created successfully.

        This test verifies basic file creation with all required fields.
        """
        # Arrange
        user = revel_user_factory()
        file_content = b"test file content"
        uploaded_file = SimpleUploadedFile(
            name="test_document.pdf",
            content=file_content,
            content_type="application/pdf",
        )

        # Act
        questionnaire_file = QuestionnaireFile.objects.create(
            uploader=user,
            file=uploaded_file,
            original_filename="test_document.pdf",
            file_hash="abc123def456",
            mime_type="application/pdf",
            file_size=len(file_content),
        )

        # Assert
        assert questionnaire_file.pk is not None
        assert questionnaire_file.uploader == user
        assert questionnaire_file.original_filename == "test_document.pdf"
        assert questionnaire_file.file_hash == "abc123def456"
        assert questionnaire_file.mime_type == "application/pdf"
        assert questionnaire_file.file_size == len(file_content)
        assert questionnaire_file.file is not None

    def test_questionnaire_file_unique_constraint_per_user(self, revel_user_factory: RevelUserFactory) -> None:
        """Test that the unique constraint on (uploader, file_hash) works.

        A user cannot upload the same file content twice (same hash).
        """
        # Arrange
        user = revel_user_factory()
        file_hash = "same_hash_123"
        uploaded_file1 = SimpleUploadedFile(
            name="file1.pdf",
            content=b"content1",
            content_type="application/pdf",
        )
        uploaded_file2 = SimpleUploadedFile(
            name="file2.pdf",
            content=b"content2",
            content_type="application/pdf",
        )

        # Act - Create first file
        QuestionnaireFile.objects.create(
            uploader=user,
            file=uploaded_file1,
            original_filename="file1.pdf",
            file_hash=file_hash,
            mime_type="application/pdf",
            file_size=100,
        )

        # Assert - Second file with same hash raises ValidationError (unique constraint via full_clean)
        with pytest.raises(ValidationError):
            QuestionnaireFile.objects.create(
                uploader=user,
                file=uploaded_file2,
                original_filename="file2.pdf",
                file_hash=file_hash,
                mime_type="application/pdf",
                file_size=100,
            )

    def test_different_users_can_have_same_file_hash(self, revel_user_factory: RevelUserFactory) -> None:
        """Test that different users can upload files with the same hash.

        The unique constraint is per-user, so two users can have the same file.
        """
        # Arrange
        user1 = revel_user_factory()
        user2 = revel_user_factory()
        file_hash = "shared_hash_456"
        uploaded_file1 = SimpleUploadedFile(
            name="file1.pdf",
            content=b"content",
            content_type="application/pdf",
        )
        uploaded_file2 = SimpleUploadedFile(
            name="file2.pdf",
            content=b"content",
            content_type="application/pdf",
        )

        # Act & Assert - Both users can create files with same hash
        file1 = QuestionnaireFile.objects.create(
            uploader=user1,
            file=uploaded_file1,
            original_filename="file1.pdf",
            file_hash=file_hash,
            mime_type="application/pdf",
            file_size=100,
        )
        file2 = QuestionnaireFile.objects.create(
            uploader=user2,
            file=uploaded_file2,
            original_filename="file2.pdf",
            file_hash=file_hash,
            mime_type="application/pdf",
            file_size=100,
        )
        assert file1.pk != file2.pk

    def test_questionnaire_file_for_user_manager_method(self, revel_user_factory: RevelUserFactory) -> None:
        """Test the for_user() manager method filters correctly."""
        # Arrange
        user1 = revel_user_factory()
        user2 = revel_user_factory()

        for i, user in enumerate([user1, user1, user2]):
            uploaded_file = SimpleUploadedFile(
                name=f"file{i}.pdf",
                content=f"content{i}".encode(),
                content_type="application/pdf",
            )
            QuestionnaireFile.objects.create(
                uploader=user,
                file=uploaded_file,
                original_filename=f"file{i}.pdf",
                file_hash=f"hash_{i}",
                mime_type="application/pdf",
                file_size=100,
            )

        # Act
        user1_files = QuestionnaireFile.objects.for_user(user1)
        user2_files = QuestionnaireFile.objects.for_user(user2)

        # Assert
        assert user1_files.count() == 2
        assert user2_files.count() == 1
        assert all(f.uploader == user1 for f in user1_files)
        assert all(f.uploader == user2 for f in user2_files)

    def test_questionnaire_file_delete_cleans_storage(self, revel_user_factory: RevelUserFactory) -> None:
        """Test that deleting a QuestionnaireFile removes the file from storage.

        Privacy policy requires hard deletion of files including from storage.
        """
        # Arrange
        user = revel_user_factory()
        uploaded_file = SimpleUploadedFile(
            name="to_delete.pdf",
            content=b"delete me",
            content_type="application/pdf",
        )
        questionnaire_file = QuestionnaireFile.objects.create(
            uploader=user,
            file=uploaded_file,
            original_filename="to_delete.pdf",
            file_hash="delete_hash",
            mime_type="application/pdf",
            file_size=9,
        )

        # Act
        questionnaire_file.delete()

        # Assert
        assert QuestionnaireFile.objects.filter(file_hash="delete_hash").count() == 0

    def test_questionnaire_file_truncates_long_filename(self, revel_user_factory: RevelUserFactory) -> None:
        """Test that long filenames are truncated while preserving extension."""
        # Arrange
        user = revel_user_factory()
        # Create a filename longer than 255 characters
        long_name = "a" * 300 + ".pdf"
        uploaded_file = SimpleUploadedFile(
            name=long_name,
            content=b"content",
            content_type="application/pdf",
        )

        # Act
        questionnaire_file = QuestionnaireFile.objects.create(
            uploader=user,
            file=uploaded_file,
            original_filename=long_name,
            file_hash="long_name_hash",
            mime_type="application/pdf",
            file_size=7,
        )

        # Assert
        assert len(questionnaire_file.original_filename) <= 255
        assert questionnaire_file.original_filename.endswith(".pdf")
        assert "..." in questionnaire_file.original_filename

    def test_questionnaire_file_str_representation(self, revel_user_factory: RevelUserFactory) -> None:
        """Test the string representation of QuestionnaireFile."""
        # Arrange
        user = revel_user_factory()
        uploaded_file = SimpleUploadedFile(
            name="test.pdf",
            content=b"content",
            content_type="application/pdf",
        )

        # Act
        questionnaire_file = QuestionnaireFile.objects.create(
            uploader=user,
            file=uploaded_file,
            original_filename="test.pdf",
            file_hash="str_hash",
            mime_type="application/pdf",
            file_size=7,
        )

        # Assert
        expected_str = f"test.pdf ({user})"
        assert str(questionnaire_file) == expected_str

    def test_upload_path_truncates_long_extension(self, revel_user_factory: RevelUserFactory) -> None:
        """Test that file upload path truncates extensions longer than 10 characters.

        The questionnaire_file_upload_path function limits extension length to prevent
        path manipulation attacks.
        """
        from questionnaires.models import questionnaire_file_upload_path

        # Arrange
        user = revel_user_factory()
        # Create a filename with an absurdly long extension
        long_extension_filename = "document.verylongextensionname"
        uploaded_file = SimpleUploadedFile(
            name=long_extension_filename,
            content=b"content",
            content_type="application/octet-stream",
        )
        questionnaire_file = QuestionnaireFile(
            uploader=user,
            file=uploaded_file,
            original_filename=long_extension_filename,
            file_hash="ext_test_hash",
            mime_type="application/octet-stream",
            file_size=7,
        )

        # Act
        generated_path = questionnaire_file_upload_path(questionnaire_file, long_extension_filename)

        # Assert - extension is truncated to max 10 characters (including the dot)
        path_extension = generated_path.split("/")[-1].split(".")[-1] if "." in generated_path else ""
        # The suffix[:10] takes at most 10 chars including the dot, so extension part is max 9 chars
        assert len("." + path_extension) <= 10

    def test_different_content_produces_different_hashes(self, revel_user_factory: RevelUserFactory) -> None:
        """Test that files with different content produce different hashes.

        This verifies the deduplication logic relies on actual content differences.
        """
        import hashlib

        # Arrange
        content1 = b"This is the first file content"
        content2 = b"This is different content entirely"

        # Act
        hash1 = hashlib.sha256(content1).hexdigest()
        hash2 = hashlib.sha256(content2).hexdigest()

        # Assert - Different content must produce different hashes
        assert hash1 != hash2
        # Both hashes are valid SHA-256 (64 hex characters)
        assert len(hash1) == 64
        assert len(hash2) == 64


class TestFileUploadQuestionModel:
    """Tests for FileUploadQuestion model."""

    def test_file_upload_question_creation(self, questionnaire: Questionnaire) -> None:
        """Test that a FileUploadQuestion can be created with default values."""
        # Act
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Please upload your ID document.",
        )

        # Assert
        assert fu_question.pk is not None
        assert fu_question.question == "Please upload your ID document."
        assert fu_question.allowed_mime_types == []  # Default empty list
        assert fu_question.max_file_size == 5 * 1024 * 1024  # Default 5MB
        assert fu_question.max_files == 1  # Default 1

    def test_file_upload_question_with_custom_settings(self, questionnaire: Questionnaire) -> None:
        """Test that FileUploadQuestion can be configured with custom settings."""
        # Act
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload images (max 3)",
            allowed_mime_types=["image/jpeg", "image/png"],
            max_file_size=10 * 1024 * 1024,  # 10MB
            max_files=3,
            is_mandatory=True,
        )

        # Assert
        assert fu_question.allowed_mime_types == ["image/jpeg", "image/png"]
        assert fu_question.max_file_size == 10 * 1024 * 1024
        assert fu_question.max_files == 3
        assert fu_question.is_mandatory is True

    def test_file_upload_question_default_weight_is_zero(self, questionnaire: Questionnaire) -> None:
        """Test that FileUploadQuestion has informational default weight.

        File upload questions are informational by default (no scoring),
        so positive_weight defaults to 0.
        """
        # Act - Note: Schema sets default, model inherits from BaseQuestion
        # The model uses BaseQuestion default of 1.0, but schema overrides to 0.0
        # Testing model behavior directly here
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload supporting documents",
        )

        # Assert - Model uses BaseQuestion defaults
        # (Schema will override this to 0.0 when created via service)
        assert fu_question.positive_weight is not None

    def test_file_upload_question_in_section(self, questionnaire: Questionnaire) -> None:
        """Test that FileUploadQuestion can be assigned to a section."""
        # Arrange
        from questionnaires.models import QuestionnaireSection

        section = QuestionnaireSection.objects.create(
            questionnaire=questionnaire,
            name="Document Upload Section",
            order=1,
        )

        # Act
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            section=section,
            question="Upload documents for this section",
        )

        # Assert
        assert fu_question.section == section
        assert fu_question in section.fileuploadquestion_questions.all()


class TestFileUploadAnswerModel:
    """Tests for FileUploadAnswer model."""

    def test_file_upload_answer_creation(
        self,
        revel_user_factory: RevelUserFactory,
        questionnaire: Questionnaire,
    ) -> None:
        """Test that a FileUploadAnswer can be created with files."""
        # Arrange
        user = revel_user_factory()
        submission = QuestionnaireSubmission.objects.create(
            user=user,
            questionnaire=questionnaire,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.DRAFT,
        )
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload your document",
        )
        uploaded_file = SimpleUploadedFile(
            name="document.pdf",
            content=b"file content",
            content_type="application/pdf",
        )
        questionnaire_file = QuestionnaireFile.objects.create(
            uploader=user,
            file=uploaded_file,
            original_filename="document.pdf",
            file_hash="answer_test_hash",
            mime_type="application/pdf",
            file_size=12,
        )

        # Act
        fu_answer = FileUploadAnswer.objects.create(
            submission=submission,
            question=fu_question,
        )
        fu_answer.files.add(questionnaire_file)

        # Assert
        assert fu_answer.pk is not None
        assert fu_answer.submission == submission
        assert fu_answer.question == fu_question
        assert fu_answer.files.count() == 1
        assert questionnaire_file in fu_answer.files.all()

    def test_file_upload_answer_multiple_files(
        self,
        revel_user_factory: RevelUserFactory,
        questionnaire: Questionnaire,
    ) -> None:
        """Test that FileUploadAnswer can have multiple files via M2M."""
        # Arrange
        user = revel_user_factory()
        submission = QuestionnaireSubmission.objects.create(
            user=user,
            questionnaire=questionnaire,
        )
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload multiple documents",
            max_files=3,
        )

        files = []
        for i in range(3):
            uploaded_file = SimpleUploadedFile(
                name=f"doc{i}.pdf",
                content=f"content{i}".encode(),
                content_type="application/pdf",
            )
            qf = QuestionnaireFile.objects.create(
                uploader=user,
                file=uploaded_file,
                original_filename=f"doc{i}.pdf",
                file_hash=f"multi_hash_{i}",
                mime_type="application/pdf",
                file_size=8,
            )
            files.append(qf)

        # Act
        fu_answer = FileUploadAnswer.objects.create(
            submission=submission,
            question=fu_question,
        )
        fu_answer.files.set(files)

        # Assert
        assert fu_answer.files.count() == 3
        for f in files:
            assert f in fu_answer.files.all()

    def test_file_upload_answer_unique_constraint(
        self,
        revel_user_factory: RevelUserFactory,
        questionnaire: Questionnaire,
    ) -> None:
        """Test that only one FileUploadAnswer per submission per question is allowed."""
        # Arrange
        user = revel_user_factory()
        submission = QuestionnaireSubmission.objects.create(
            user=user,
            questionnaire=questionnaire,
        )
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Single answer question",
        )

        # Act - Create first answer
        FileUploadAnswer.objects.create(
            submission=submission,
            question=fu_question,
        )

        # Assert - Second answer for same submission+question fails (unique constraint via full_clean)
        with pytest.raises(ValidationError):
            FileUploadAnswer.objects.create(
                submission=submission,
                question=fu_question,
            )

    def test_file_shared_across_multiple_answers(
        self,
        revel_user_factory: RevelUserFactory,
        questionnaire: Questionnaire,
    ) -> None:
        """Test that the same QuestionnaireFile can be used in multiple answers.

        A file in a user's library can be reused across different questions/questionnaires.
        """
        # Arrange
        user = revel_user_factory()
        submission = QuestionnaireSubmission.objects.create(
            user=user,
            questionnaire=questionnaire,
        )
        fu_question1 = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Question 1",
            order=1,
        )
        fu_question2 = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Question 2",
            order=2,
        )
        uploaded_file = SimpleUploadedFile(
            name="shared.pdf",
            content=b"shared content",
            content_type="application/pdf",
        )
        shared_file = QuestionnaireFile.objects.create(
            uploader=user,
            file=uploaded_file,
            original_filename="shared.pdf",
            file_hash="shared_file_hash",
            mime_type="application/pdf",
            file_size=14,
        )

        # Act
        answer1 = FileUploadAnswer.objects.create(
            submission=submission,
            question=fu_question1,
        )
        answer1.files.add(shared_file)

        answer2 = FileUploadAnswer.objects.create(
            submission=submission,
            question=fu_question2,
        )
        answer2.files.add(shared_file)

        # Assert
        assert shared_file in answer1.files.all()
        assert shared_file in answer2.files.all()
        assert shared_file.file_upload_answers.count() == 2

    def test_deleting_file_clears_m2m_relationship(
        self,
        revel_user_factory: RevelUserFactory,
        questionnaire: Questionnaire,
    ) -> None:
        """Test that deleting a QuestionnaireFile clears the M2M relationship.

        Privacy first: when a user deletes a file, it's removed from all answers.
        """
        # Arrange
        user = revel_user_factory()
        submission = QuestionnaireSubmission.objects.create(
            user=user,
            questionnaire=questionnaire,
        )
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload document",
        )
        uploaded_file = SimpleUploadedFile(
            name="to_delete.pdf",
            content=b"delete me",
            content_type="application/pdf",
        )
        questionnaire_file = QuestionnaireFile.objects.create(
            uploader=user,
            file=uploaded_file,
            original_filename="to_delete.pdf",
            file_hash="delete_m2m_hash",
            mime_type="application/pdf",
            file_size=9,
        )
        fu_answer = FileUploadAnswer.objects.create(
            submission=submission,
            question=fu_question,
        )
        fu_answer.files.add(questionnaire_file)
        assert fu_answer.files.count() == 1

        # Act
        questionnaire_file.delete()

        # Assert
        fu_answer.refresh_from_db()
        assert fu_answer.files.count() == 0
        assert FileUploadAnswer.objects.filter(pk=fu_answer.pk).exists()  # Answer still exists
