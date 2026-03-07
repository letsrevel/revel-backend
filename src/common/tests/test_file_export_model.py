"""Tests for FileExport model, export_service helpers, and cleanup task.

Tests cover:
- FileExport creation with all statuses and export types
- Status transitions via export_service helpers (start_export, complete_export, fail_export)
- cleanup_expired_file_exports task (deletes files older than 7 days)
"""

from datetime import timedelta

import pytest
from django.core.files.base import ContentFile
from django.utils import timezone

from accounts.models import RevelUser
from common.models import FileExport
from common.service.export_service import complete_export, fail_export, start_export
from common.tasks import cleanup_expired_file_exports
from conftest import RevelUserFactory

pytestmark = pytest.mark.django_db


# --- Fixtures ---


@pytest.fixture
def export_user(revel_user_factory: RevelUserFactory) -> RevelUser:
    """User who requests exports."""
    return revel_user_factory(username="exporter")


@pytest.fixture
def pending_export(export_user: RevelUser) -> FileExport:
    """A freshly created PENDING export."""
    return FileExport.objects.create(
        requested_by=export_user,
        export_type=FileExport.ExportType.QUESTIONNAIRE_SUBMISSIONS,
        parameters={"questionnaire_id": "00000000-0000-0000-0000-000000000001"},
    )


# --- FileExport Model Creation Tests ---


class TestFileExportCreation:
    """Tests for FileExport model creation and field defaults."""

    def test_create_questionnaire_export(self, export_user: RevelUser) -> None:
        """FileExport for questionnaire submissions should be created with PENDING status."""
        export = FileExport.objects.create(
            requested_by=export_user,
            export_type=FileExport.ExportType.QUESTIONNAIRE_SUBMISSIONS,
            parameters={"questionnaire_id": "test-uuid"},
        )

        assert export.status == FileExport.ExportStatus.PENDING
        assert export.export_type == FileExport.ExportType.QUESTIONNAIRE_SUBMISSIONS
        assert export.file.name is None or export.file.name == ""
        assert export.error_message is None
        assert export.completed_at is None
        assert export.parameters == {"questionnaire_id": "test-uuid"}

    def test_create_attendee_list_export(self, export_user: RevelUser) -> None:
        """FileExport for attendee list should be created with correct type."""
        export = FileExport.objects.create(
            requested_by=export_user,
            export_type=FileExport.ExportType.ATTENDEE_LIST,
            parameters={"event_id": "event-uuid"},
        )

        assert export.export_type == FileExport.ExportType.ATTENDEE_LIST
        assert export.status == FileExport.ExportStatus.PENDING

    def test_default_parameters_is_empty_dict(self, export_user: RevelUser) -> None:
        """Parameters field should default to an empty dict."""
        export = FileExport.objects.create(
            requested_by=export_user,
            export_type=FileExport.ExportType.ATTENDEE_LIST,
        )

        assert export.parameters == {}

    def test_str_representation(self, pending_export: FileExport) -> None:
        """String representation should include export_type and status."""
        result = str(pending_export)
        assert "questionnaire_submissions" in result
        assert "PENDING" in result


# --- Export Service Helper Tests ---


class TestStartExport:
    """Tests for start_export helper."""

    def test_transitions_to_processing(self, pending_export: FileExport) -> None:
        """start_export should set status to PROCESSING."""
        start_export(pending_export)

        pending_export.refresh_from_db()
        assert pending_export.status == FileExport.ExportStatus.PROCESSING

    def test_persists_status_change(self, pending_export: FileExport) -> None:
        """Status change should be persisted to the database."""
        start_export(pending_export)

        fresh = FileExport.objects.get(pk=pending_export.pk)
        assert fresh.status == FileExport.ExportStatus.PROCESSING


class TestCompleteExport:
    """Tests for complete_export helper."""

    def test_transitions_to_ready_with_file(self, pending_export: FileExport) -> None:
        """complete_export should set status to READY and attach file."""
        start_export(pending_export)
        file_bytes = b"fake-excel-content"
        filename = "test_export.xlsx"

        complete_export(pending_export, file_bytes, filename)

        pending_export.refresh_from_db()
        assert pending_export.status == FileExport.ExportStatus.READY
        assert pending_export.file is not None
        assert pending_export.file.name != ""
        assert pending_export.completed_at is not None

    def test_file_content_is_stored(self, pending_export: FileExport) -> None:
        """The exported file content should be stored and readable."""
        start_export(pending_export)
        content = b"spreadsheet-data"

        complete_export(pending_export, content, "export.xlsx")

        pending_export.refresh_from_db()
        stored_content = pending_export.file.read()
        assert stored_content == content

    def test_completed_at_is_set(self, pending_export: FileExport) -> None:
        """completed_at should be set to approximately the current time."""
        start_export(pending_export)
        before = timezone.now()

        complete_export(pending_export, b"data", "export.xlsx")

        pending_export.refresh_from_db()
        assert pending_export.completed_at is not None
        assert pending_export.completed_at >= before


class TestFailExport:
    """Tests for fail_export helper."""

    def test_transitions_to_failed_with_error(self, pending_export: FileExport) -> None:
        """fail_export should set status to FAILED and store error message."""
        start_export(pending_export)

        fail_export(pending_export, "Something went wrong")

        pending_export.refresh_from_db()
        assert pending_export.status == FileExport.ExportStatus.FAILED
        assert pending_export.error_message == "Something went wrong"

    def test_no_file_on_failure(self, pending_export: FileExport) -> None:
        """Failed exports should not have a file attached."""
        start_export(pending_export)
        fail_export(pending_export, "error")

        pending_export.refresh_from_db()
        assert not pending_export.file


# --- Full Lifecycle Tests ---


class TestExportLifecycle:
    """Tests for the complete export status transition lifecycle."""

    def test_pending_to_processing_to_ready(self, pending_export: FileExport) -> None:
        """Test the happy path: PENDING -> PROCESSING -> READY."""
        assert pending_export.status == FileExport.ExportStatus.PENDING

        start_export(pending_export)
        pending_export.refresh_from_db()
        assert pending_export.status == FileExport.ExportStatus.PROCESSING

        complete_export(pending_export, b"data", "file.xlsx")
        pending_export.refresh_from_db()
        assert pending_export.status == FileExport.ExportStatus.READY

    def test_pending_to_processing_to_failed(self, pending_export: FileExport) -> None:
        """Test the failure path: PENDING -> PROCESSING -> FAILED."""
        assert pending_export.status == FileExport.ExportStatus.PENDING

        start_export(pending_export)
        pending_export.refresh_from_db()
        assert pending_export.status == FileExport.ExportStatus.PROCESSING

        fail_export(pending_export, "Generation failed")
        pending_export.refresh_from_db()
        assert pending_export.status == FileExport.ExportStatus.FAILED
        assert pending_export.error_message == "Generation failed"


# --- Cleanup Task Tests ---


class TestCleanupExpiredFileExports:
    """Tests for cleanup_expired_file_exports task."""

    def test_deletes_expired_ready_exports(self, export_user: RevelUser) -> None:
        """READY exports completed more than 7 days ago should be fully deleted."""
        export = FileExport.objects.create(
            requested_by=export_user,
            export_type=FileExport.ExportType.ATTENDEE_LIST,
            status=FileExport.ExportStatus.READY,
        )
        export.file.save("old_export.xlsx", ContentFile(b"old-data"), save=False)
        export.completed_at = timezone.now() - timedelta(days=8)
        export.save(update_fields=["file", "completed_at"])
        export_id = export.pk

        result = cleanup_expired_file_exports()

        assert result["records_deleted"] == 1
        assert not FileExport.objects.filter(pk=export_id).exists()

    def test_preserves_recent_exports(self, export_user: RevelUser) -> None:
        """Exports completed less than 7 days ago should be preserved."""
        export = FileExport.objects.create(
            requested_by=export_user,
            export_type=FileExport.ExportType.ATTENDEE_LIST,
            status=FileExport.ExportStatus.READY,
        )
        export.file.save("recent_export.xlsx", ContentFile(b"recent-data"), save=False)
        export.completed_at = timezone.now() - timedelta(days=3)
        export.save(update_fields=["file", "completed_at"])

        result = cleanup_expired_file_exports()

        assert result["records_deleted"] == 0
        export.refresh_from_db()
        assert export.file

    def test_deletes_old_failed_exports(self, export_user: RevelUser) -> None:
        """FAILED exports older than 7 days should be deleted."""
        export = FileExport.objects.create(
            requested_by=export_user,
            export_type=FileExport.ExportType.ATTENDEE_LIST,
            status=FileExport.ExportStatus.FAILED,
            error_message="Something broke",
        )
        FileExport.objects.filter(pk=export.pk).update(
            updated_at=timezone.now() - timedelta(days=8),
        )
        export_id = export.pk

        result = cleanup_expired_file_exports()

        assert result["records_deleted"] == 1
        assert not FileExport.objects.filter(pk=export_id).exists()

    def test_preserves_recent_failed_exports(self, export_user: RevelUser) -> None:
        """Recent FAILED exports should be preserved for debugging."""
        FileExport.objects.create(
            requested_by=export_user,
            export_type=FileExport.ExportType.ATTENDEE_LIST,
            status=FileExport.ExportStatus.FAILED,
            error_message="Something broke",
        )

        result = cleanup_expired_file_exports()

        assert result["records_deleted"] == 0

    def test_deletes_expired_ready_without_file(self, export_user: RevelUser) -> None:
        """READY exports without files should also be deleted after expiry."""
        export = FileExport.objects.create(
            requested_by=export_user,
            export_type=FileExport.ExportType.ATTENDEE_LIST,
            status=FileExport.ExportStatus.READY,
            completed_at=timezone.now() - timedelta(days=10),
        )
        export_id = export.pk

        result = cleanup_expired_file_exports()

        assert result["records_deleted"] == 1
        assert not FileExport.objects.filter(pk=export_id).exists()

    def test_multiple_expired_exports(self, export_user: RevelUser) -> None:
        """Should clean up multiple expired exports in one run."""
        for i in range(3):
            export = FileExport.objects.create(
                requested_by=export_user,
                export_type=FileExport.ExportType.QUESTIONNAIRE_SUBMISSIONS,
                status=FileExport.ExportStatus.READY,
            )
            export.file.save(f"export_{i}.xlsx", ContentFile(f"data-{i}".encode()), save=False)
            export.completed_at = timezone.now() - timedelta(days=10)
            export.save(update_fields=["file", "completed_at"])

        result = cleanup_expired_file_exports()

        assert result["records_deleted"] == 3
        assert FileExport.objects.count() == 0

    def test_mixed_expired_and_recent(self, export_user: RevelUser) -> None:
        """Only expired exports should be deleted."""
        # Expired
        old_export = FileExport.objects.create(
            requested_by=export_user,
            export_type=FileExport.ExportType.ATTENDEE_LIST,
            status=FileExport.ExportStatus.READY,
        )
        old_export.file.save("old.xlsx", ContentFile(b"old"), save=False)
        old_export.completed_at = timezone.now() - timedelta(days=10)
        old_export.save(update_fields=["file", "completed_at"])

        # Recent
        new_export = FileExport.objects.create(
            requested_by=export_user,
            export_type=FileExport.ExportType.ATTENDEE_LIST,
            status=FileExport.ExportStatus.READY,
        )
        new_export.file.save("new.xlsx", ContentFile(b"new"), save=False)
        new_export.completed_at = timezone.now() - timedelta(days=2)
        new_export.save(update_fields=["file", "completed_at"])

        result = cleanup_expired_file_exports()

        assert result["records_deleted"] == 1
        assert not FileExport.objects.filter(pk=old_export.pk).exists()
        new_export.refresh_from_db()
        assert new_export.file
