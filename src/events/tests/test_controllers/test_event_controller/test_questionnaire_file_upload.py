"""Tests for questionnaire submission with file uploads via event controller.

This module tests the integration of file upload answers when submitting
questionnaires through the event controller endpoints.
"""

from unittest.mock import MagicMock, patch

import orjson
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from accounts.models import RevelUser
from events.models import Event, Organization, OrganizationQuestionnaire
from questionnaires.models import (
    FileUploadQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireFile,
    QuestionnaireSubmission,
)

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def questionnaire_with_file_upload(organization: Organization, public_event: Event) -> Questionnaire:
    """A questionnaire with MC and FU questions linked to the public_event."""
    q = Questionnaire.objects.create(
        name="FU Test Questionnaire",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
    )
    # Add one mandatory MCQ
    mcq = MultipleChoiceQuestion.objects.create(questionnaire=q, question="Mandatory MCQ", is_mandatory=True)
    MultipleChoiceOption.objects.create(question=mcq, option="Correct", is_correct=True)

    # Add one optional file upload question
    FileUploadQuestion.objects.create(
        questionnaire=q,
        question="Optional file upload",
        is_mandatory=False,
        order=2,
    )

    # Link to event
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=q)
    org_q.events.add(public_event)
    return q


@pytest.fixture
def questionnaire_with_mandatory_file_upload(organization: Organization, public_event: Event) -> Questionnaire:
    """A questionnaire with a mandatory file upload question."""
    q = Questionnaire.objects.create(
        name="Mandatory FU Questionnaire",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
    )
    # Add one mandatory file upload question
    FileUploadQuestion.objects.create(
        questionnaire=q,
        question="Required document upload",
        is_mandatory=True,
        order=1,
    )
    # Link to event
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=q)
    org_q.events.add(public_event)
    return q


@pytest.fixture
def questionnaire_with_mime_restricted_upload(organization: Organization, public_event: Event) -> Questionnaire:
    """A questionnaire with a MIME-type restricted file upload question."""
    q = Questionnaire.objects.create(
        name="MIME Restricted FU Questionnaire",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
    )
    # Add file upload question that only accepts images
    FileUploadQuestion.objects.create(
        questionnaire=q,
        question="Upload images only",
        allowed_mime_types=["image/jpeg", "image/png"],
        is_mandatory=True,
        order=1,
    )
    # Link to event
    org_q = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=q)
    org_q.events.add(public_event)
    return q


@pytest.fixture
def user_questionnaire_file(nonmember_user: RevelUser) -> QuestionnaireFile:
    """A file in the nonmember user's library."""
    uploaded_file = SimpleUploadedFile(
        name="user_file.pdf",
        content=b"user file content",
        content_type="application/pdf",
    )
    return QuestionnaireFile.objects.create(
        uploader=nonmember_user,
        file=uploaded_file,
        original_filename="user_file.pdf",
        file_hash="nonmember_hash_001",
        mime_type="application/pdf",
        file_size=17,
    )


@pytest.fixture
def user_image_file(nonmember_user: RevelUser, png_bytes: bytes) -> QuestionnaireFile:
    """An image file in the nonmember user's library."""
    uploaded_file = SimpleUploadedFile(
        name="user_image.png",
        content=png_bytes,
        content_type="image/png",
    )
    return QuestionnaireFile.objects.create(
        uploader=nonmember_user,
        file=uploaded_file,
        original_filename="user_image.png",
        file_hash="nonmember_image_hash",
        mime_type="image/png",
        file_size=len(png_bytes),
    )


# --- Tests for submission with file upload answers ---


class TestSubmitQuestionnaireWithFileUpload:
    """Tests for submitting questionnaires with file upload answers."""

    @patch("events.controllers.event_public.attendance.evaluate_questionnaire_submission.delay")
    def test_submit_with_optional_file_upload(
        self,
        mock_evaluate_task: MagicMock,
        nonmember_client: Client,
        public_event: Event,
        questionnaire_with_file_upload: Questionnaire,
        user_questionnaire_file: QuestionnaireFile,
    ) -> None:
        """Test successful submission with an optional file upload answer."""
        # Arrange
        mcq = questionnaire_with_file_upload.multiplechoicequestion_questions.first()
        option = mcq.options.first()  # type: ignore[union-attr]
        fu_question = questionnaire_with_file_upload.fileuploadquestion_questions.first()

        url = reverse(
            "api:submit_questionnaire",
            kwargs={
                "event_id": public_event.pk,
                "questionnaire_id": questionnaire_with_file_upload.pk,
            },
        )
        payload = {
            "questionnaire_id": str(questionnaire_with_file_upload.pk),
            "status": "ready",
            "multiple_choice_answers": [
                {"question_id": str(mcq.id), "options_id": [str(option.id)]}  # type: ignore[union-attr]
            ],
            "file_upload_answers": [
                {"question_id": str(fu_question.id), "file_ids": [str(user_questionnaire_file.id)]}  # type: ignore[union-attr]
            ],
        }

        # Act
        response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        submission = QuestionnaireSubmission.objects.first()
        assert submission is not None
        assert submission.fileuploadanswer_answers.count() == 1
        fu_answer = submission.fileuploadanswer_answers.first()
        assert fu_answer is not None
        assert fu_answer.files.count() == 1

    @patch("events.controllers.event_public.attendance.evaluate_questionnaire_submission.delay")
    def test_submit_without_optional_file_upload(
        self,
        mock_evaluate_task: MagicMock,
        nonmember_client: Client,
        public_event: Event,
        questionnaire_with_file_upload: Questionnaire,
    ) -> None:
        """Test successful submission without providing optional file upload."""
        # Arrange
        mcq = questionnaire_with_file_upload.multiplechoicequestion_questions.first()
        option = mcq.options.first()  # type: ignore[union-attr]

        url = reverse(
            "api:submit_questionnaire",
            kwargs={
                "event_id": public_event.pk,
                "questionnaire_id": questionnaire_with_file_upload.pk,
            },
        )
        payload = {
            "questionnaire_id": str(questionnaire_with_file_upload.pk),
            "status": "ready",
            "multiple_choice_answers": [
                {"question_id": str(mcq.id), "options_id": [str(option.id)]}  # type: ignore[union-attr]
            ],
            # No file_upload_answers - it's optional
        }

        # Act
        response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        # Assert
        assert response.status_code == 200
        submission = QuestionnaireSubmission.objects.first()
        assert submission is not None
        assert submission.fileuploadanswer_answers.count() == 0

    def test_submit_fails_missing_mandatory_file_upload(
        self,
        nonmember_client: Client,
        public_event: Event,
        questionnaire_with_mandatory_file_upload: Questionnaire,
    ) -> None:
        """Test that submission fails when mandatory file upload is missing."""
        # Arrange
        url = reverse(
            "api:submit_questionnaire",
            kwargs={
                "event_id": public_event.pk,
                "questionnaire_id": questionnaire_with_mandatory_file_upload.pk,
            },
        )
        payload = {
            "questionnaire_id": str(questionnaire_with_mandatory_file_upload.pk),
            "status": "ready",
            "file_upload_answers": [],  # Missing mandatory file upload
        }

        # Act
        response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        # Assert
        assert response.status_code == 400
        assert "mandatory" in response.json()["detail"].lower()

    def test_submit_fails_with_invalid_mime_type(
        self,
        nonmember_client: Client,
        public_event: Event,
        questionnaire_with_mime_restricted_upload: Questionnaire,
        user_questionnaire_file: QuestionnaireFile,  # PDF file, but question only accepts images
    ) -> None:
        """Test that submission fails when file MIME type is not allowed."""
        # Arrange
        fu_question = questionnaire_with_mime_restricted_upload.fileuploadquestion_questions.first()

        url = reverse(
            "api:submit_questionnaire",
            kwargs={
                "event_id": public_event.pk,
                "questionnaire_id": questionnaire_with_mime_restricted_upload.pk,
            },
        )
        payload = {
            "questionnaire_id": str(questionnaire_with_mime_restricted_upload.pk),
            "status": "ready",
            "file_upload_answers": [
                {
                    "question_id": str(fu_question.id),  # type: ignore[union-attr]
                    "file_ids": [str(user_questionnaire_file.id)],  # PDF, not image
                }
            ],
        }

        # Act
        response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        # Assert
        assert response.status_code == 400
        # The exception handler returns a specific message for InvalidFileMimeTypeError
        assert "has type" in response.json()["detail"]
        assert "which is not allowed" in response.json()["detail"]

    @patch("events.controllers.event_public.attendance.evaluate_questionnaire_submission.delay")
    def test_submit_succeeds_with_valid_mime_type(
        self,
        mock_evaluate_task: MagicMock,
        nonmember_client: Client,
        public_event: Event,
        questionnaire_with_mime_restricted_upload: Questionnaire,
        user_image_file: QuestionnaireFile,  # PNG file
    ) -> None:
        """Test successful submission with valid MIME type."""
        # Arrange
        fu_question = questionnaire_with_mime_restricted_upload.fileuploadquestion_questions.first()

        url = reverse(
            "api:submit_questionnaire",
            kwargs={
                "event_id": public_event.pk,
                "questionnaire_id": questionnaire_with_mime_restricted_upload.pk,
            },
        )
        payload = {
            "questionnaire_id": str(questionnaire_with_mime_restricted_upload.pk),
            "status": "ready",
            "file_upload_answers": [
                {
                    "question_id": str(fu_question.id),  # type: ignore[union-attr]
                    "file_ids": [str(user_image_file.id)],  # PNG is allowed
                }
            ],
        }

        # Act
        response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        # Assert
        assert response.status_code == 200
        submission = QuestionnaireSubmission.objects.first()
        assert submission is not None
        assert submission.fileuploadanswer_answers.count() == 1

    def test_submit_fails_with_another_users_file(
        self,
        nonmember_client: Client,
        public_event: Event,
        questionnaire_with_mandatory_file_upload: Questionnaire,
        organization_owner_user: RevelUser,  # Different user
    ) -> None:
        """Test that submission fails when using another user's file."""
        # Arrange - Create file owned by org owner, not nonmember
        other_file = SimpleUploadedFile(
            name="other_user_file.pdf",
            content=b"other content",
            content_type="application/pdf",
        )
        other_users_file = QuestionnaireFile.objects.create(
            uploader=organization_owner_user,
            file=other_file,
            original_filename="other_user_file.pdf",
            file_hash="other_user_hash",
            mime_type="application/pdf",
            file_size=13,
        )

        fu_question = questionnaire_with_mandatory_file_upload.fileuploadquestion_questions.first()

        url = reverse(
            "api:submit_questionnaire",
            kwargs={
                "event_id": public_event.pk,
                "questionnaire_id": questionnaire_with_mandatory_file_upload.pk,
            },
        )
        payload = {
            "questionnaire_id": str(questionnaire_with_mandatory_file_upload.pk),
            "status": "ready",
            "file_upload_answers": [
                {
                    "question_id": str(fu_question.id),  # type: ignore[union-attr]
                    "file_ids": [str(other_users_file.id)],
                }
            ],
        }

        # Act
        response = nonmember_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        # Assert
        assert response.status_code == 400
        # The exception handler returns a specific message for FileOwnershipError
        assert response.json()["detail"] == "Some files do not exist or do not belong to you."


class TestGetQuestionnaireIncludesFileUploadQuestions:
    """Tests for GET questionnaire endpoint including file upload questions."""

    def test_get_questionnaire_includes_file_upload_questions(
        self,
        nonmember_client: Client,
        public_event: Event,
        questionnaire_with_file_upload: Questionnaire,
    ) -> None:
        """Test that get questionnaire returns file upload questions in schema."""
        # Arrange
        url = reverse(
            "api:get_questionnaire",
            kwargs={
                "event_id": public_event.pk,
                "questionnaire_id": questionnaire_with_file_upload.pk,
            },
        )

        # Act
        response = nonmember_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert len(data["file_upload_questions"]) == 1
        fu_q = data["file_upload_questions"][0]
        assert fu_q["question"] == "Optional file upload"
        assert "allowed_mime_types" in fu_q
        assert "max_file_size" in fu_q
        assert "max_files" in fu_q
