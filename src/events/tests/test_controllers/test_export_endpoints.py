"""Tests for export API endpoints.

Tests cover:
- POST /questionnaires/{id}/submissions/export (202 response, permission checks)
- POST /event-admin/{event_id}/export-attendees (202 response, permission checks)
- GET /exports/{export_id} (polling, user scoping)
"""

import typing as t
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from common.models import FileExport
from events.models import (
    Event,
    Organization,
    OrganizationQuestionnaire,
    OrganizationStaff,
    PermissionMap,
    PermissionsSchema,
)
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def export_owner(django_user_model: type[RevelUser]) -> RevelUser:
    """Organization owner for export tests."""
    return django_user_model.objects.create_user(
        username="export_owner",
        email="export_owner@example.com",
        password="pass",
        email_verified=True,
    )


@pytest.fixture
def export_owner_client(export_owner: RevelUser) -> Client:
    """Authenticated client for the export owner."""
    refresh = RefreshToken.for_user(export_owner)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def export_org(export_owner: RevelUser) -> Organization:
    """Organization for export tests."""
    return Organization.objects.create(
        name="Export Test Org",
        slug="export-test-org",
        owner=export_owner,
    )


@pytest.fixture
def export_event(export_org: Organization) -> Event:
    """Event for export tests."""
    from django.utils import timezone

    return Event.objects.create(
        organization=export_org,
        name="Export Event",
        slug="export-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        start=timezone.now(),
        status="open",
        requires_ticket=True,
    )


@pytest.fixture
def export_questionnaire() -> Questionnaire:
    """Published questionnaire for export tests."""
    return Questionnaire.objects.create(
        name="Export Questionnaire",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
    )


@pytest.fixture
def export_org_questionnaire(
    export_org: Organization, export_questionnaire: Questionnaire
) -> OrganizationQuestionnaire:
    """Link the questionnaire to the organization."""
    return OrganizationQuestionnaire.objects.create(organization=export_org, questionnaire=export_questionnaire)


@pytest.fixture
def staff_with_evaluate(
    django_user_model: type[RevelUser],
    export_org: Organization,
) -> RevelUser:
    """Staff user with evaluate_questionnaire permission."""
    user = django_user_model.objects.create_user(username="eval_staff", email="eval_staff@example.com", password="pass")
    OrganizationStaff.objects.create(
        organization=export_org,
        user=user,
        permissions=PermissionsSchema(default=PermissionMap(evaluate_questionnaire=True)).model_dump(mode="json"),
    )
    return user


@pytest.fixture
def staff_with_evaluate_client(staff_with_evaluate: RevelUser) -> Client:
    """Authenticated client for staff with evaluate permission."""
    refresh = RefreshToken.for_user(staff_with_evaluate)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def staff_without_evaluate(
    django_user_model: type[RevelUser],
    export_org: Organization,
) -> RevelUser:
    """Staff user without evaluate_questionnaire permission."""
    user = django_user_model.objects.create_user(username="no_eval_staff", email="no_eval@example.com", password="pass")
    OrganizationStaff.objects.create(
        organization=export_org,
        user=user,
        permissions=PermissionsSchema(
            default=PermissionMap(evaluate_questionnaire=False, manage_event=False)
        ).model_dump(mode="json"),
    )
    return user


@pytest.fixture
def staff_without_evaluate_client(staff_without_evaluate: RevelUser) -> Client:
    """Authenticated client for staff without evaluate permission."""
    refresh = RefreshToken.for_user(staff_without_evaluate)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def other_user(django_user_model: type[RevelUser]) -> RevelUser:
    """An unrelated user with no org access."""
    return django_user_model.objects.create_user(username="other_user", email="other@example.com", password="pass")


@pytest.fixture
def other_user_client(other_user: RevelUser) -> Client:
    """Authenticated client for unrelated user."""
    refresh = RefreshToken.for_user(other_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


# --- Questionnaire Export Endpoint Tests ---


class TestExportSubmissionsEndpoint:
    """Tests for POST /questionnaires/{id}/submissions/export."""

    @patch("events.tasks.generate_questionnaire_export_task")
    def test_returns_202_for_owner(
        self,
        mock_task: t.Any,
        export_owner_client: Client,
        export_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Owner should receive 202 and a FileExport object."""
        url = reverse(
            "api:export_submissions",
            kwargs={"org_questionnaire_id": export_org_questionnaire.id},
        )
        response = export_owner_client.post(url, content_type="application/json")

        assert response.status_code == 202, response.content
        data = response.json()
        assert "id" in data
        assert data["status"] == "PENDING"
        assert data["export_type"] == "questionnaire_submissions"

    @patch("events.tasks.generate_questionnaire_export_task")
    def test_creates_file_export_record(
        self,
        mock_task: t.Any,
        export_owner_client: Client,
        export_org_questionnaire: OrganizationQuestionnaire,
        export_owner: RevelUser,
    ) -> None:
        """A FileExport record should be created in the database."""
        url = reverse(
            "api:export_submissions",
            kwargs={"org_questionnaire_id": export_org_questionnaire.id},
        )
        response = export_owner_client.post(url, content_type="application/json")
        data = response.json()

        export = FileExport.objects.get(pk=data["id"])
        assert export.requested_by == export_owner
        assert export.export_type == FileExport.ExportType.QUESTIONNAIRE_SUBMISSIONS
        assert "questionnaire_id" in export.parameters

    @patch("events.tasks.generate_questionnaire_export_task")
    def test_triggers_celery_task(
        self,
        mock_task: t.Any,
        export_owner_client: Client,
        export_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """The export should trigger the async Celery task."""
        url = reverse(
            "api:export_submissions",
            kwargs={"org_questionnaire_id": export_org_questionnaire.id},
        )
        export_owner_client.post(url, content_type="application/json")

        mock_task.delay.assert_called_once()

    @patch("events.tasks.generate_questionnaire_export_task")
    def test_staff_with_evaluate_permission_allowed(
        self,
        mock_task: t.Any,
        staff_with_evaluate_client: Client,
        export_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Staff with evaluate_questionnaire permission should be allowed."""
        url = reverse(
            "api:export_submissions",
            kwargs={"org_questionnaire_id": export_org_questionnaire.id},
        )
        response = staff_with_evaluate_client.post(url, content_type="application/json")

        assert response.status_code == 202

    def test_staff_without_evaluate_permission_denied(
        self,
        staff_without_evaluate_client: Client,
        export_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Staff without evaluate_questionnaire permission should be denied."""
        url = reverse(
            "api:export_submissions",
            kwargs={"org_questionnaire_id": export_org_questionnaire.id},
        )
        response = staff_without_evaluate_client.post(url, content_type="application/json")

        assert response.status_code == 403

    def test_unrelated_user_denied(
        self,
        other_user_client: Client,
        export_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """User with no org access should not find the resource."""
        url = reverse(
            "api:export_submissions",
            kwargs={"org_questionnaire_id": export_org_questionnaire.id},
        )
        response = other_user_client.post(url, content_type="application/json")

        assert response.status_code == 404

    def test_unauthenticated_denied(
        self,
        export_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Unauthenticated request should be denied."""
        url = reverse(
            "api:export_submissions",
            kwargs={"org_questionnaire_id": export_org_questionnaire.id},
        )
        client = Client()
        response = client.post(url, content_type="application/json")

        assert response.status_code == 401

    @patch("events.tasks.generate_questionnaire_export_task")
    def test_with_event_id_filter(
        self,
        mock_task: t.Any,
        export_owner_client: Client,
        export_org_questionnaire: OrganizationQuestionnaire,
        export_event: Event,
    ) -> None:
        """Export request with event_id should store it in parameters."""
        url = reverse(
            "api:export_submissions",
            kwargs={"org_questionnaire_id": export_org_questionnaire.id},
        )
        response = export_owner_client.post(
            f"{url}?event_id={export_event.id}",
            content_type="application/json",
        )

        assert response.status_code == 202
        data = response.json()
        export = FileExport.objects.get(pk=data["id"])
        assert export.parameters.get("event_id") == str(export_event.id)


# --- Attendee Export Endpoint Tests ---


class TestExportAttendeesEndpoint:
    """Tests for POST /event-admin/{event_id}/export-attendees."""

    @patch("events.tasks.generate_attendee_export_task")
    def test_returns_202_for_owner(
        self,
        mock_task: t.Any,
        export_owner_client: Client,
        export_event: Event,
    ) -> None:
        """Owner should receive 202 and a FileExport object."""
        url = reverse("api:export_attendees", kwargs={"event_id": export_event.id})
        response = export_owner_client.post(url, content_type="application/json")

        assert response.status_code == 202, response.content
        data = response.json()
        assert "id" in data
        assert data["status"] == "PENDING"
        assert data["export_type"] == "attendee_list"

    @patch("events.tasks.generate_attendee_export_task")
    def test_creates_file_export_record(
        self,
        mock_task: t.Any,
        export_owner_client: Client,
        export_event: Event,
        export_owner: RevelUser,
    ) -> None:
        """A FileExport record should be created in the database."""
        url = reverse("api:export_attendees", kwargs={"event_id": export_event.id})
        response = export_owner_client.post(url, content_type="application/json")
        data = response.json()

        export = FileExport.objects.get(pk=data["id"])
        assert export.requested_by == export_owner
        assert export.export_type == FileExport.ExportType.ATTENDEE_LIST
        assert export.parameters["event_id"] == str(export_event.id)

    @patch("events.tasks.generate_attendee_export_task")
    def test_triggers_celery_task(
        self,
        mock_task: t.Any,
        export_owner_client: Client,
        export_event: Event,
    ) -> None:
        """The export should trigger the async Celery task."""
        url = reverse("api:export_attendees", kwargs={"event_id": export_event.id})
        export_owner_client.post(url, content_type="application/json")

        mock_task.delay.assert_called_once()

    def test_staff_without_manage_event_denied(
        self,
        staff_without_evaluate_client: Client,
        export_event: Event,
    ) -> None:
        """Staff without manage_event permission should be denied."""
        url = reverse("api:export_attendees", kwargs={"event_id": export_event.id})
        response = staff_without_evaluate_client.post(url, content_type="application/json")

        assert response.status_code == 403

    def test_unrelated_user_denied(
        self,
        other_user_client: Client,
        export_event: Event,
    ) -> None:
        """User with no org access should be denied."""
        url = reverse("api:export_attendees", kwargs={"event_id": export_event.id})
        response = other_user_client.post(url, content_type="application/json")

        assert response.status_code == 403

    def test_unauthenticated_denied(
        self,
        export_event: Event,
    ) -> None:
        """Unauthenticated request should be denied."""
        url = reverse("api:export_attendees", kwargs={"event_id": export_event.id})
        client = Client()
        response = client.post(url, content_type="application/json")

        assert response.status_code == 401


# --- Export Status Polling Endpoint Tests ---


class TestGetExportStatusEndpoint:
    """Tests for GET /exports/{export_id}."""

    def test_returns_pending_status(
        self,
        export_owner_client: Client,
        export_owner: RevelUser,
    ) -> None:
        """GET should return the current export status."""
        export = FileExport.objects.create(
            requested_by=export_owner,
            export_type=FileExport.ExportType.QUESTIONNAIRE_SUBMISSIONS,
            parameters={"questionnaire_id": str(uuid4())},
        )

        url = reverse("api:get_export_status", kwargs={"export_id": export.id})
        response = export_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(export.id)
        assert data["status"] == "PENDING"
        assert data["download_url"] is None

    def test_returns_ready_status_with_download_url(
        self,
        export_owner_client: Client,
        export_owner: RevelUser,
    ) -> None:
        """When export is READY, response should include a download_url."""
        from django.core.files.base import ContentFile
        from django.utils import timezone

        export = FileExport.objects.create(
            requested_by=export_owner,
            export_type=FileExport.ExportType.ATTENDEE_LIST,
            status=FileExport.ExportStatus.READY,
            completed_at=timezone.now(),
        )
        export.file.save("test.xlsx", ContentFile(b"data"), save=True)

        url = reverse("api:get_export_status", kwargs={"export_id": export.id})
        response = export_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "READY"
        assert data["download_url"] is not None
        assert "sig=" in data["download_url"]

    def test_returns_failed_status_with_error(
        self,
        export_owner_client: Client,
        export_owner: RevelUser,
    ) -> None:
        """When export has FAILED, response should include error_message."""
        export = FileExport.objects.create(
            requested_by=export_owner,
            export_type=FileExport.ExportType.QUESTIONNAIRE_SUBMISSIONS,
            status=FileExport.ExportStatus.FAILED,
            error_message="Something went wrong",
        )

        url = reverse("api:get_export_status", kwargs={"export_id": export.id})
        response = export_owner_client.get(url)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "FAILED"
        assert data["error_message"] == "Something went wrong"
        assert data["download_url"] is None

    def test_user_scoping_cannot_see_others_export(
        self,
        other_user_client: Client,
        export_owner: RevelUser,
    ) -> None:
        """Users should not be able to see exports requested by others."""
        export = FileExport.objects.create(
            requested_by=export_owner,
            export_type=FileExport.ExportType.QUESTIONNAIRE_SUBMISSIONS,
            parameters={"questionnaire_id": str(uuid4())},
        )

        url = reverse("api:get_export_status", kwargs={"export_id": export.id})
        response = other_user_client.get(url)

        assert response.status_code == 404

    def test_unauthenticated_denied(self, export_owner: RevelUser) -> None:
        """Unauthenticated request should be denied."""
        export = FileExport.objects.create(
            requested_by=export_owner,
            export_type=FileExport.ExportType.ATTENDEE_LIST,
        )

        url = reverse("api:get_export_status", kwargs={"export_id": export.id})
        client = Client()
        response = client.get(url)

        assert response.status_code == 401

    def test_nonexistent_export_returns_404(
        self,
        export_owner_client: Client,
    ) -> None:
        """Requesting a non-existent export ID should return 404."""
        url = reverse("api:get_export_status", kwargs={"export_id": uuid4()})
        response = export_owner_client.get(url)

        assert response.status_code == 404

    def test_response_schema_fields(
        self,
        export_owner_client: Client,
        export_owner: RevelUser,
    ) -> None:
        """Response should contain all expected FileExportSchema fields."""
        export = FileExport.objects.create(
            requested_by=export_owner,
            export_type=FileExport.ExportType.QUESTIONNAIRE_SUBMISSIONS,
        )

        url = reverse("api:get_export_status", kwargs={"export_id": export.id})
        response = export_owner_client.get(url)

        data = response.json()
        expected_fields = {"id", "export_type", "status", "error_message", "completed_at", "created_at", "download_url"}
        assert expected_fields == set(data.keys())
