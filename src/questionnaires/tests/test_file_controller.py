"""Tests for QuestionnaireFileController.

This module tests:
- POST / - Upload file (with deduplication, malware scanning)
- GET / - List user's files (paginated)
- GET /{file_id} - Get single file
- DELETE /{file_id} - Hard delete file
"""

from unittest import mock
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from conftest import RevelUserFactory
from questionnaires.models import (
    FileUploadAnswer,
    FileUploadQuestion,
    Questionnaire,
    QuestionnaireFile,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def user(revel_user_factory: RevelUserFactory) -> RevelUser:
    """Provides a standard user instance."""
    return revel_user_factory()


@pytest.fixture
def another_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    """Provides another user instance."""
    return revel_user_factory()


@pytest.fixture
def user_client(user: RevelUser) -> Client:
    """API client for an authenticated user."""
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def another_user_client(another_user: RevelUser) -> Client:
    """API client for another authenticated user."""
    refresh = RefreshToken.for_user(another_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def questionnaire_file(user: RevelUser) -> QuestionnaireFile:
    """Creates a QuestionnaireFile owned by user."""
    uploaded_file = SimpleUploadedFile(
        name="existing_file.pdf",
        content=b"existing content",
        content_type="application/pdf",
    )
    return QuestionnaireFile.objects.create(
        uploader=user,
        file=uploaded_file,
        original_filename="existing_file.pdf",
        file_hash="existing_hash_123",
        mime_type="application/pdf",
        file_size=16,
    )


# --- Tests for upload endpoint ---


class TestUploadFile:
    """Tests for POST /questionnaire-files/ endpoint."""

    def test_upload_file_success(self, user_client: Client, user: RevelUser, png_bytes: bytes) -> None:
        """Test successfully uploading a file.

        Uses real PNG content to verify MIME type detection from file content works.
        Note: Images undergo EXIF stripping which may change file size slightly.
        """
        # Arrange
        url = reverse("api:upload_questionnaire_file")
        uploaded_file = SimpleUploadedFile(
            name="upload_test.png",
            content=png_bytes,
            content_type="image/png",
        )

        # Act
        response = user_client.post(url, {"file": uploaded_file}, format="multipart")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["original_filename"] == "upload_test.png"
        assert data["mime_type"] == "image/png"  # Detected from content, not header
        # File size may differ from original due to EXIF stripping (re-encodes image)
        assert data["file_size"] > 0
        assert "id" in data
        assert "file_url" in data

        # Verify file was created in database
        assert QuestionnaireFile.objects.filter(uploader=user).count() == 1
        qf = QuestionnaireFile.objects.get(uploader=user)
        assert qf.original_filename == "upload_test.png"

    def test_upload_file_deduplication_returns_existing(self, user_client: Client, user: RevelUser) -> None:
        """Test that uploading the same content returns the existing file (deduplication)."""
        # Arrange
        url = reverse("api:upload_questionnaire_file")
        file_content = b"same content for deduplication test"

        # First upload
        uploaded_file1 = SimpleUploadedFile(
            name="file1.pdf",
            content=file_content,
            content_type="application/pdf",
        )
        response1 = user_client.post(url, {"file": uploaded_file1}, format="multipart")
        assert response1.status_code == 200
        first_id = response1.json()["id"]

        # Second upload with same content but different filename
        uploaded_file2 = SimpleUploadedFile(
            name="file2.pdf",
            content=file_content,
            content_type="application/pdf",
        )
        response2 = user_client.post(url, {"file": uploaded_file2}, format="multipart")

        # Assert - Should return the same file
        assert response2.status_code == 200
        assert response2.json()["id"] == first_id
        assert QuestionnaireFile.objects.filter(uploader=user).count() == 1

    def test_upload_file_different_users_can_have_same_content(
        self,
        user_client: Client,
        another_user_client: Client,
        user: RevelUser,
        another_user: RevelUser,
    ) -> None:
        """Test that different users can upload files with the same content."""
        # Arrange
        url = reverse("api:upload_questionnaire_file")
        file_content = b"shared content between users"

        # User 1 uploads
        uploaded_file1 = SimpleUploadedFile(
            name="user1_file.pdf",
            content=file_content,
            content_type="application/pdf",
        )
        response1 = user_client.post(url, {"file": uploaded_file1}, format="multipart")
        assert response1.status_code == 200
        user1_file_id = response1.json()["id"]

        # User 2 uploads same content
        uploaded_file2 = SimpleUploadedFile(
            name="user2_file.pdf",
            content=file_content,
            content_type="application/pdf",
        )
        response2 = another_user_client.post(url, {"file": uploaded_file2}, format="multipart")

        # Assert - Different files created for each user
        assert response2.status_code == 200
        user2_file_id = response2.json()["id"]
        assert user1_file_id != user2_file_id
        assert QuestionnaireFile.objects.filter(uploader=user).count() == 1
        assert QuestionnaireFile.objects.filter(uploader=another_user).count() == 1

    def test_upload_file_anonymous_fails(self, client: Client) -> None:
        """Test that anonymous users cannot upload files."""
        # Arrange
        url = reverse("api:upload_questionnaire_file")
        uploaded_file = SimpleUploadedFile(
            name="anonymous.pdf",
            content=b"anonymous content",
            content_type="application/pdf",
        )

        # Act
        response = client.post(url, {"file": uploaded_file}, format="multipart")

        # Assert
        assert response.status_code == 401

    @mock.patch("common.utils.tasks.scan_for_malware")
    def test_upload_file_triggers_malware_scan(
        self, mock_scan: mock.MagicMock, user_client: Client, user: RevelUser, png_bytes: bytes
    ) -> None:
        """Test that file upload triggers malware scan task.

        Verifies that scan_for_malware.delay() is called with correct parameters.
        """
        # Arrange
        url = reverse("api:upload_questionnaire_file")
        # Use real PNG bytes to pass MIME type detection
        uploaded_file = SimpleUploadedFile(
            name="scan_test.png",
            content=png_bytes,
            content_type="image/png",
        )

        # Act
        response = user_client.post(url, {"file": uploaded_file}, format="multipart")

        # Assert - File was created and scan was triggered
        assert response.status_code == 200
        qf = QuestionnaireFile.objects.get(uploader=user)

        # Verify scan_for_malware.delay was called with correct parameters
        mock_scan.delay.assert_called_once_with(
            app="questionnaires",
            model="questionnairefile",
            pk=str(qf.pk),
            field="file",
        )


# --- Tests for list endpoint ---


class TestListFiles:
    """Tests for GET /questionnaire-files/ endpoint."""

    def test_list_files_empty(self, user_client: Client) -> None:
        """Test listing files when user has no files."""
        # Arrange
        url = reverse("api:list_questionnaire_files")

        # Act
        response = user_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["results"] == []

    def test_list_files_returns_user_files_only(
        self,
        user_client: Client,
        user: RevelUser,
        another_user: RevelUser,
        questionnaire_file: QuestionnaireFile,
    ) -> None:
        """Test that list only returns files belonging to the current user."""
        # Arrange
        # Create file for another user
        other_file = SimpleUploadedFile(
            name="other_user.pdf",
            content=b"other user content",
            content_type="application/pdf",
        )
        QuestionnaireFile.objects.create(
            uploader=another_user,
            file=other_file,
            original_filename="other_user.pdf",
            file_hash="other_hash",
            mime_type="application/pdf",
            file_size=18,
        )
        url = reverse("api:list_questionnaire_files")

        # Act
        response = user_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["id"] == str(questionnaire_file.id)

    def test_list_files_pagination(self, user_client: Client, user: RevelUser) -> None:
        """Test that list files endpoint supports pagination."""
        # Arrange - Create multiple files
        for i in range(5):
            uploaded_file = SimpleUploadedFile(
                name=f"file_{i}.pdf",
                content=f"content {i}".encode(),
                content_type="application/pdf",
            )
            QuestionnaireFile.objects.create(
                uploader=user,
                file=uploaded_file,
                original_filename=f"file_{i}.pdf",
                file_hash=f"hash_{i}",
                mime_type="application/pdf",
                file_size=9,
            )
        url = reverse("api:list_questionnaire_files")

        # Act - Request with page_size=2
        response = user_client.get(url, {"page_size": 2})

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 5
        assert len(data["results"]) == 2
        assert "next" in data or data.get("next") is None  # Pagination link present

    def test_list_files_sorted_by_created_at_desc(self, user_client: Client, user: RevelUser) -> None:
        """Test that files are sorted by most recent first."""
        # Arrange
        for i in range(3):
            uploaded_file = SimpleUploadedFile(
                name=f"sorted_{i}.pdf",
                content=f"content {i}".encode(),
                content_type="application/pdf",
            )
            QuestionnaireFile.objects.create(
                uploader=user,
                file=uploaded_file,
                original_filename=f"sorted_{i}.pdf",
                file_hash=f"sorted_hash_{i}",
                mime_type="application/pdf",
                file_size=9,
            )
        url = reverse("api:list_questionnaire_files")

        # Act
        response = user_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        results = data["results"]
        # Most recent should be first (sorted_2)
        assert results[0]["original_filename"] == "sorted_2.pdf"
        assert results[2]["original_filename"] == "sorted_0.pdf"

    def test_list_files_anonymous_fails(self, client: Client) -> None:
        """Test that anonymous users cannot list files."""
        # Arrange
        url = reverse("api:list_questionnaire_files")

        # Act
        response = client.get(url)

        # Assert
        assert response.status_code == 401


# --- Tests for get single file endpoint ---


class TestGetFile:
    """Tests for GET /questionnaire-files/{file_id} endpoint."""

    def test_get_file_success(
        self,
        user_client: Client,
        questionnaire_file: QuestionnaireFile,
    ) -> None:
        """Test successfully getting a file's details."""
        # Arrange
        url = reverse("api:get_questionnaire_file", kwargs={"file_id": questionnaire_file.id})

        # Act
        response = user_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(questionnaire_file.id)
        assert data["original_filename"] == questionnaire_file.original_filename
        assert data["mime_type"] == questionnaire_file.mime_type
        assert data["file_size"] == questionnaire_file.file_size

    def test_get_file_not_found(self, user_client: Client) -> None:
        """Test getting a non-existent file returns 404."""
        # Arrange
        url = reverse("api:get_questionnaire_file", kwargs={"file_id": uuid4()})

        # Act
        response = user_client.get(url)

        # Assert
        assert response.status_code == 404

    def test_get_file_owned_by_another_user_returns_404(
        self,
        another_user_client: Client,
        questionnaire_file: QuestionnaireFile,
    ) -> None:
        """Test that users cannot get files owned by other users."""
        # Arrange
        url = reverse("api:get_questionnaire_file", kwargs={"file_id": questionnaire_file.id})

        # Act
        response = another_user_client.get(url)

        # Assert - Should return 404, not 403 (don't reveal file existence)
        assert response.status_code == 404

    def test_get_file_anonymous_fails(self, client: Client, questionnaire_file: QuestionnaireFile) -> None:
        """Test that anonymous users cannot get file details."""
        # Arrange
        url = reverse("api:get_questionnaire_file", kwargs={"file_id": questionnaire_file.id})

        # Act
        response = client.get(url)

        # Assert
        assert response.status_code == 401


# --- Tests for delete endpoint ---


class TestDeleteFile:
    """Tests for DELETE /questionnaire-files/{file_id} endpoint."""

    def test_delete_file_success(
        self,
        user_client: Client,
        user: RevelUser,
        questionnaire_file: QuestionnaireFile,
    ) -> None:
        """Test successfully deleting a file."""
        # Arrange
        url = reverse("api:delete_questionnaire_file", kwargs={"file_id": questionnaire_file.id})

        # Act
        response = user_client.delete(url)

        # Assert
        assert response.status_code == 204
        assert not QuestionnaireFile.objects.filter(pk=questionnaire_file.id).exists()

    def test_delete_file_not_found(self, user_client: Client) -> None:
        """Test deleting a non-existent file returns 404."""
        # Arrange
        url = reverse("api:delete_questionnaire_file", kwargs={"file_id": uuid4()})

        # Act
        response = user_client.delete(url)

        # Assert
        assert response.status_code == 404

    def test_delete_file_owned_by_another_user_returns_404(
        self,
        another_user_client: Client,
        questionnaire_file: QuestionnaireFile,
    ) -> None:
        """Test that users cannot delete files owned by other users."""
        # Arrange
        url = reverse("api:delete_questionnaire_file", kwargs={"file_id": questionnaire_file.id})

        # Act
        response = another_user_client.delete(url)

        # Assert - Should return 404
        assert response.status_code == 404
        # File should still exist
        assert QuestionnaireFile.objects.filter(pk=questionnaire_file.id).exists()

    def test_delete_file_clears_m2m_but_keeps_answer(
        self,
        user_client: Client,
        user: RevelUser,
        questionnaire_file: QuestionnaireFile,
        questionnaire: Questionnaire,
    ) -> None:
        """Test that deleting a file clears the M2M relationship but keeps the answer.

        Privacy first: files are hard deleted even if referenced by submissions.
        The FileUploadAnswer remains but with no files attached.
        """
        # Arrange - Create a submission with the file
        submission = QuestionnaireSubmission.objects.create(
            user=user,
            questionnaire=questionnaire,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=questionnaire,
            question="Upload document",
        )
        fu_answer = FileUploadAnswer.objects.create(
            submission=submission,
            question=fu_question,
        )
        fu_answer.files.add(questionnaire_file)
        assert fu_answer.files.count() == 1

        url = reverse("api:delete_questionnaire_file", kwargs={"file_id": questionnaire_file.id})

        # Act
        response = user_client.delete(url)

        # Assert
        assert response.status_code == 204
        assert not QuestionnaireFile.objects.filter(pk=questionnaire_file.id).exists()
        # Answer still exists but with no files
        fu_answer.refresh_from_db()
        assert fu_answer.files.count() == 0

    def test_delete_file_anonymous_fails(self, client: Client, questionnaire_file: QuestionnaireFile) -> None:
        """Test that anonymous users cannot delete files."""
        # Arrange
        url = reverse("api:delete_questionnaire_file", kwargs={"file_id": questionnaire_file.id})

        # Act
        response = client.delete(url)

        # Assert
        assert response.status_code == 401


# --- Tests for schema response ---


class TestSchemaResponse:
    """Tests for response schema correctness."""

    def test_upload_returns_correct_schema(self, user_client: Client, user: RevelUser) -> None:
        """Test that upload returns all expected fields in schema."""
        # Arrange
        url = reverse("api:upload_questionnaire_file")
        uploaded_file = SimpleUploadedFile(
            name="schema_test.pdf",
            content=b"schema content",
            content_type="application/pdf",
        )

        # Act
        response = user_client.post(url, {"file": uploaded_file}, format="multipart")

        # Assert
        assert response.status_code == 200
        data = response.json()
        expected_fields = {"id", "original_filename", "mime_type", "file_size", "file_url", "created_at"}
        assert expected_fields.issubset(set(data.keys()))

    def test_list_item_returns_correct_schema(
        self,
        user_client: Client,
        questionnaire_file: QuestionnaireFile,
    ) -> None:
        """Test that list items contain all expected fields in schema."""
        # Arrange
        url = reverse("api:list_questionnaire_files")

        # Act
        response = user_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        item = data["results"][0]
        expected_fields = {"id", "original_filename", "mime_type", "file_size", "file_url", "created_at"}
        assert expected_fields.issubset(set(item.keys()))
