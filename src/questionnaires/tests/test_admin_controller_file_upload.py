"""Tests for file upload question endpoints in QuestionnaireController (admin).

This module tests:
- POST /questionnaires/{id}/file-upload-questions - Create FU question
- PUT /questionnaires/{id}/file-upload-questions/{question_id} - Update FU question
- DELETE /questionnaires/{id}/file-upload-questions/{question_id} - Delete FU question
- GET /questionnaires/{id}/submissions/{id} - Submission detail includes FU answers
"""

from uuid import uuid4

import orjson
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Organization,
    OrganizationQuestionnaire,
)
from questionnaires.models import (
    FileUploadAnswer,
    FileUploadQuestion,
    Questionnaire,
    QuestionnaireFile,
    QuestionnaireSection,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def org_owner(revel_user_factory: RevelUserFactory) -> RevelUser:
    """Organization owner user."""
    return revel_user_factory()


@pytest.fixture
def organization(org_owner: RevelUser) -> Organization:
    """Test organization."""
    return Organization.objects.create(
        name="Test Org",
        slug="test-org",
        owner=org_owner,
    )


@pytest.fixture
def org_owner_client(org_owner: RevelUser) -> Client:
    """API client for organization owner."""
    refresh = RefreshToken.for_user(org_owner)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def org_questionnaire(organization: Organization) -> OrganizationQuestionnaire:
    """Organization questionnaire for testing."""
    questionnaire = Questionnaire.objects.create(
        name="Test Questionnaire",
        status=Questionnaire.QuestionnaireStatus.DRAFT,
    )
    return OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=questionnaire,
    )


@pytest.fixture
def section(org_questionnaire: OrganizationQuestionnaire) -> QuestionnaireSection:
    """Section in the test questionnaire."""
    return QuestionnaireSection.objects.create(
        questionnaire=org_questionnaire.questionnaire,
        name="Test Section",
        order=1,
    )


# --- Tests for create_fu_question ---


class TestCreateFileUploadQuestion:
    """Tests for POST /questionnaires/{id}/file-upload-questions endpoint."""

    def test_create_fu_question_success(
        self,
        org_owner_client: Client,
        org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Test successfully creating a file upload question."""
        # Arrange
        url = reverse(
            "api:create_fu_question",
            kwargs={"org_questionnaire_id": org_questionnaire.id},
        )
        payload = {
            "question": "Please upload your ID document",
            "hint": "Valid formats: PDF, JPEG, PNG",
            "is_mandatory": True,
            "allowed_mime_types": ["application/pdf", "image/jpeg", "image/png"],
            "max_file_size": 10485760,  # 10MB
            "max_files": 2,
        }

        # Act
        response = org_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["question"] == "Please upload your ID document"
        assert data["hint"] == "Valid formats: PDF, JPEG, PNG"
        assert data["is_mandatory"] is True
        assert data["allowed_mime_types"] == ["application/pdf", "image/jpeg", "image/png"]
        assert data["max_file_size"] == 10485760
        assert data["max_files"] == 2

        # Verify database
        fu_question = FileUploadQuestion.objects.get(pk=data["id"])
        assert fu_question.questionnaire == org_questionnaire.questionnaire

    def test_create_fu_question_with_defaults(
        self,
        org_owner_client: Client,
        org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Test creating a file upload question with default values."""
        # Arrange
        url = reverse(
            "api:create_fu_question",
            kwargs={"org_questionnaire_id": org_questionnaire.id},
        )
        payload = {
            "question": "Optional file upload",
        }

        # Act
        response = org_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["allowed_mime_types"] == []  # Allow all
        assert data["max_file_size"] == 5 * 1024 * 1024  # 5MB default
        assert data["max_files"] == 1

    def test_create_fu_question_in_section(
        self,
        org_owner_client: Client,
        org_questionnaire: OrganizationQuestionnaire,
        section: QuestionnaireSection,
    ) -> None:
        """Test creating a file upload question in a section."""
        # Arrange
        url = reverse(
            "api:create_fu_question",
            kwargs={"org_questionnaire_id": org_questionnaire.id},
        )
        payload = {
            "question": "Section file upload",
            "section_id": str(section.id),
        }

        # Act
        response = org_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["section_id"] == str(section.id)

        # Verify database
        fu_question = FileUploadQuestion.objects.get(pk=data["id"])
        assert fu_question.section == section

    def test_create_fu_question_unauthorized(
        self,
        client: Client,
        org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Test that anonymous users cannot create file upload questions."""
        # Arrange
        url = reverse(
            "api:create_fu_question",
            kwargs={"org_questionnaire_id": org_questionnaire.id},
        )
        payload = {"question": "Anonymous upload"}

        # Act
        response = client.post(url, data=orjson.dumps(payload), content_type="application/json")

        # Assert
        assert response.status_code == 401


# --- Tests for update_fu_question ---


class TestUpdateFileUploadQuestion:
    """Tests for PUT /questionnaires/{id}/file-upload-questions/{id} endpoint."""

    def test_update_fu_question_success(
        self,
        org_owner_client: Client,
        org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Test successfully updating a file upload question."""
        # Arrange
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=org_questionnaire.questionnaire,
            question="Original question",
        )
        url = reverse(
            "api:update_fu_question",
            kwargs={
                "org_questionnaire_id": org_questionnaire.id,
                "question_id": fu_question.id,
            },
        )
        payload = {
            "question": "Updated question text",
            "hint": "New hint",
            "is_mandatory": True,
            "allowed_mime_types": ["image/jpeg"],
            "max_file_size": 20971520,  # 20MB
            "max_files": 5,
        }

        # Act
        response = org_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["question"] == "Updated question text"
        assert data["hint"] == "New hint"
        assert data["is_mandatory"] is True
        assert data["allowed_mime_types"] == ["image/jpeg"]
        assert data["max_file_size"] == 20971520
        assert data["max_files"] == 5

    def test_update_fu_question_move_to_section(
        self,
        org_owner_client: Client,
        org_questionnaire: OrganizationQuestionnaire,
        section: QuestionnaireSection,
    ) -> None:
        """Test moving a file upload question to a section."""
        # Arrange
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=org_questionnaire.questionnaire,
            question="Move me to section",
        )
        url = reverse(
            "api:update_fu_question",
            kwargs={
                "org_questionnaire_id": org_questionnaire.id,
                "question_id": fu_question.id,
            },
        )
        payload = {
            "question": fu_question.question,
            "section_id": str(section.id),
        }

        # Act
        response = org_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["section_id"] == str(section.id)

    def test_update_fu_question_not_found(
        self,
        org_owner_client: Client,
        org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Test updating a non-existent file upload question returns 404."""
        # Arrange
        url = reverse(
            "api:update_fu_question",
            kwargs={
                "org_questionnaire_id": org_questionnaire.id,
                "question_id": uuid4(),
            },
        )
        payload = {"question": "Not found"}

        # Act
        response = org_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        # Assert
        assert response.status_code == 404


# --- Tests for delete_fu_question ---


class TestDeleteFileUploadQuestion:
    """Tests for DELETE /questionnaires/{id}/file-upload-questions/{id} endpoint."""

    def test_delete_fu_question_success(
        self,
        org_owner_client: Client,
        org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Test successfully deleting a file upload question."""
        # Arrange
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=org_questionnaire.questionnaire,
            question="Delete me",
        )
        url = reverse(
            "api:delete_fu_question",
            kwargs={
                "org_questionnaire_id": org_questionnaire.id,
                "question_id": fu_question.id,
            },
        )

        # Act
        response = org_owner_client.delete(url)

        # Assert
        assert response.status_code == 204
        assert not FileUploadQuestion.objects.filter(pk=fu_question.id).exists()

    def test_delete_fu_question_not_found(
        self,
        org_owner_client: Client,
        org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Test deleting a non-existent file upload question returns 404."""
        # Arrange
        url = reverse(
            "api:delete_fu_question",
            kwargs={
                "org_questionnaire_id": org_questionnaire.id,
                "question_id": uuid4(),
            },
        )

        # Act
        response = org_owner_client.delete(url)

        # Assert
        assert response.status_code == 404


# --- Tests for submission detail with file upload answers ---


class TestSubmissionDetailWithFileUploadAnswers:
    """Tests for GET /questionnaires/{id}/submissions/{id} with file upload answers."""

    def test_submission_detail_includes_file_upload_answers(
        self,
        org_owner_client: Client,
        org_owner: RevelUser,
        org_questionnaire: OrganizationQuestionnaire,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that submission detail endpoint includes file upload answers."""
        # Arrange - Create submission with file upload answer
        submitter = revel_user_factory()
        submission = QuestionnaireSubmission.objects.create(
            user=submitter,
            questionnaire=org_questionnaire.questionnaire,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=org_questionnaire.questionnaire,
            question="Upload document",
        )
        uploaded_file = SimpleUploadedFile(
            name="submission_file.pdf",
            content=b"submission content",
            content_type="application/pdf",
        )
        qf = QuestionnaireFile.objects.create(
            uploader=submitter,
            file=uploaded_file,
            original_filename="submission_file.pdf",
            file_hash="submission_hash",
            mime_type="application/pdf",
            file_size=18,
        )
        fu_answer = FileUploadAnswer.objects.create(
            submission=submission,
            question=fu_question,
        )
        fu_answer.files.add(qf)

        url = reverse(
            "api:get_submission_detail",
            kwargs={
                "org_questionnaire_id": org_questionnaire.id,
                "submission_id": submission.id,
            },
        )

        # Act
        response = org_owner_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data["answers"]) == 1
        fu_answer_data = data["answers"][0]
        assert fu_answer_data["question_type"] == "file_upload"
        assert fu_answer_data["question_text"] == "Upload document"
        assert len(fu_answer_data["answer_content"]) == 1
        file_data = fu_answer_data["answer_content"][0]
        assert file_data["original_filename"] == "submission_file.pdf"
        assert file_data["mime_type"] == "application/pdf"
        assert file_data["file_size"] == 18
        assert "file_url" in file_data

    def test_submission_detail_multiple_files_in_answer(
        self,
        org_owner_client: Client,
        org_owner: RevelUser,
        org_questionnaire: OrganizationQuestionnaire,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test submission detail with multiple files in a single answer."""
        # Arrange
        submitter = revel_user_factory()
        submission = QuestionnaireSubmission.objects.create(
            user=submitter,
            questionnaire=org_questionnaire.questionnaire,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=org_questionnaire.questionnaire,
            question="Upload multiple",
            max_files=3,
        )

        files = []
        for i in range(2):
            uploaded_file = SimpleUploadedFile(
                name=f"multi_file_{i}.pdf",
                content=f"content {i}".encode(),
                content_type="application/pdf",
            )
            qf = QuestionnaireFile.objects.create(
                uploader=submitter,
                file=uploaded_file,
                original_filename=f"multi_file_{i}.pdf",
                file_hash=f"multi_hash_{i}",
                mime_type="application/pdf",
                file_size=9,
            )
            files.append(qf)

        fu_answer = FileUploadAnswer.objects.create(
            submission=submission,
            question=fu_question,
        )
        fu_answer.files.set(files)

        url = reverse(
            "api:get_submission_detail",
            kwargs={
                "org_questionnaire_id": org_questionnaire.id,
                "submission_id": submission.id,
            },
        )

        # Act
        response = org_owner_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data["answers"]) == 1
        fu_answer_data = data["answers"][0]
        assert len(fu_answer_data["answer_content"]) == 2

    def test_submission_detail_deleted_file_shows_as_unavailable(
        self,
        org_owner_client: Client,
        org_owner: RevelUser,
        org_questionnaire: OrganizationQuestionnaire,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that deleted files show as unavailable in submission detail.

        Privacy first: files can be deleted by users even if referenced in submissions.
        The answer remains but file is marked as unavailable.
        """
        # Arrange
        submitter = revel_user_factory()
        submission = QuestionnaireSubmission.objects.create(
            user=submitter,
            questionnaire=org_questionnaire.questionnaire,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        fu_question = FileUploadQuestion.objects.create(
            questionnaire=org_questionnaire.questionnaire,
            question="Upload document",
        )
        uploaded_file = SimpleUploadedFile(
            name="will_delete.pdf",
            content=b"delete me",
            content_type="application/pdf",
        )
        qf = QuestionnaireFile.objects.create(
            uploader=submitter,
            file=uploaded_file,
            original_filename="will_delete.pdf",
            file_hash="will_delete_hash",
            mime_type="application/pdf",
            file_size=9,
        )
        fu_answer = FileUploadAnswer.objects.create(
            submission=submission,
            question=fu_question,
        )
        fu_answer.files.add(qf)

        # Delete the file
        qf.delete()

        url = reverse(
            "api:get_submission_detail",
            kwargs={
                "org_questionnaire_id": org_questionnaire.id,
                "submission_id": submission.id,
            },
        )

        # Act
        response = org_owner_client.get(url)

        # Assert - Answer exists but no files
        assert response.status_code == 200
        data = response.json()
        assert len(data["answers"]) == 1
        fu_answer_data = data["answers"][0]
        assert fu_answer_data["question_type"] == "file_upload"
        assert len(fu_answer_data["answer_content"]) == 0  # File was deleted
