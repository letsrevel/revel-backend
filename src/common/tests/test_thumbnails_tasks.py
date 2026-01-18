"""Tests for the thumbnail generation Celery tasks.

This module tests the Celery tasks and integration workflows.
"""

from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from PIL import Image

from common.thumbnails.tasks import (
    ThumbnailConfigError,
    ThumbnailTargetNotFoundError,
    delete_orphaned_thumbnails_task,
    generate_thumbnails_task,
)
from conftest import RevelUserFactory
from events.models import Organization

pytestmark = pytest.mark.django_db


# =============================================================================
# Tests for generate_thumbnails_task()
# =============================================================================


class TestGenerateThumbnailsTask:
    """Tests for the generate_thumbnails_task Celery task."""

    def test_generates_thumbnails_for_valid_instance(
        self,
        revel_user_factory: RevelUserFactory,
        rgb_image_bytes: bytes,
    ) -> None:
        """Test that thumbnails are generated and model is updated."""
        owner = revel_user_factory()
        org = Organization.objects.create(name="Test Org", owner=owner)

        logo_path = f"logos/{org.pk}/logo.jpg"
        saved_path = default_storage.save(logo_path, ContentFile(rgb_image_bytes))
        org.logo = saved_path
        org.save(update_fields=["logo"])

        try:
            result = generate_thumbnails_task(
                app="events",
                model="organization",
                pk=str(org.pk),
                field="logo",
            )

            assert result is not None
            assert "logo_thumbnail" in result

            org.refresh_from_db()
            assert org.logo_thumbnail
            assert org.logo_thumbnail.name
            assert default_storage.exists(org.logo_thumbnail.name)
        finally:
            if default_storage.exists(saved_path):
                default_storage.delete(saved_path)
            if org.logo_thumbnail and default_storage.exists(org.logo_thumbnail.name):
                default_storage.delete(org.logo_thumbnail.name)

    def test_raises_config_error_for_missing_config(
        self,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that task raises ThumbnailConfigError when no config exists."""
        owner = revel_user_factory()
        org = Organization.objects.create(name="Test Org", owner=owner)

        with pytest.raises(ThumbnailConfigError) as exc_info:
            generate_thumbnails_task(
                app="events",
                model="organization",
                pk=str(org.pk),
                field="nonexistent_field",
            )

        assert "nonexistent_field" in str(exc_info.value)

    def test_raises_config_error_for_missing_model(self) -> None:
        """Test that task raises ThumbnailConfigError for nonexistent app/model.

        Note: Config check happens before model lookup, so nonexistent app/model
        combinations raise ThumbnailConfigError (no config found), not LookupError.
        This is intentional - we fail fast if there's no config.
        """
        with pytest.raises(ThumbnailConfigError):
            generate_thumbnails_task(
                app="nonexistent_app",
                model="nonexistent_model",
                pk="123",
                field="logo",
            )

    def test_raises_target_not_found_for_missing_instance(self) -> None:
        """Test that task raises ThumbnailTargetNotFoundError when instance doesn't exist."""
        with pytest.raises(ThumbnailTargetNotFoundError) as exc_info:
            generate_thumbnails_task(
                app="events",
                model="organization",
                pk="00000000-0000-0000-0000-000000000000",
                field="logo",
            )

        assert "not found" in str(exc_info.value)

    def test_raises_target_not_found_for_empty_file_field(
        self,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that task raises ThumbnailTargetNotFoundError when file field is empty."""
        owner = revel_user_factory()
        org = Organization.objects.create(name="Test Org", owner=owner)

        with pytest.raises(ThumbnailTargetNotFoundError) as exc_info:
            generate_thumbnails_task(
                app="events",
                model="organization",
                pk=str(org.pk),
                field="logo",
            )

        assert "empty" in str(exc_info.value)

    def test_raises_file_not_found_for_missing_file(
        self,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        """Test that task raises FileNotFoundError when original file doesn't exist."""
        owner = revel_user_factory()
        org = Organization.objects.create(name="Test Org", owner=owner)

        # Set a path that doesn't exist using direct DB update
        # to bypass ExifStripMixin which would try to access the file
        Organization.objects.filter(pk=org.pk).update(logo="logos/nonexistent.jpg")

        with pytest.raises(FileNotFoundError):
            generate_thumbnails_task(
                app="events",
                model="organization",
                pk=str(org.pk),
                field="logo",
            )

    @patch("common.thumbnails.tasks.generate_and_save_thumbnails")
    def test_retries_on_transient_errors(
        self,
        mock_generate: MagicMock,
        revel_user_factory: RevelUserFactory,
        rgb_image_bytes: bytes,
    ) -> None:
        """Test that task retries on transient errors (OSError)."""
        owner = revel_user_factory()
        org = Organization.objects.create(name="Test Org", owner=owner)

        logo_path = f"logos/{org.pk}/logo.jpg"
        saved_path = default_storage.save(logo_path, ContentFile(rgb_image_bytes))
        org.logo = saved_path
        org.save(update_fields=["logo"])

        mock_generate.side_effect = OSError("Storage connection failed")

        try:
            # OSError triggers autoretry_for, which wraps in Retry
            with pytest.raises((Retry, OSError)):
                generate_thumbnails_task(
                    app="events",
                    model="organization",
                    pk=str(org.pk),
                    field="logo",
                )
        finally:
            if default_storage.exists(saved_path):
                default_storage.delete(saved_path)


# =============================================================================
# Tests for delete_orphaned_thumbnails_task()
# =============================================================================


class TestDeleteOrphanedThumbnailsTask:
    """Tests for the delete_orphaned_thumbnails_task Celery task."""

    def test_deletes_thumbnails(self, rgb_image_bytes: bytes) -> None:
        """Test that task deletes specified thumbnail paths."""
        path1 = "test-thumbnails/orphan1.jpg"
        path2 = "test-thumbnails/orphan2.jpg"
        default_storage.save(path1, ContentFile(rgb_image_bytes))
        default_storage.save(path2, ContentFile(rgb_image_bytes))

        delete_orphaned_thumbnails_task(thumbnail_paths=[path1, path2])

        assert not default_storage.exists(path1)
        assert not default_storage.exists(path2)

    def test_handles_empty_list(self) -> None:
        """Test that task handles empty list gracefully."""
        delete_orphaned_thumbnails_task(thumbnail_paths=[])

    def test_handles_nonexistent_paths(self) -> None:
        """Test that task handles nonexistent paths gracefully."""
        delete_orphaned_thumbnails_task(thumbnail_paths=["nonexistent1.jpg", "nonexistent2.jpg"])


# =============================================================================
# Integration Tests
# =============================================================================


class TestThumbnailIntegration:
    """Integration tests for the thumbnail system."""

    def test_full_thumbnail_workflow(
        self,
        revel_user_factory: RevelUserFactory,
        large_image_bytes: bytes,
    ) -> None:
        """Test the complete thumbnail workflow from upload to deletion."""
        owner = revel_user_factory()
        org = Organization.objects.create(name="Integration Test Org", owner=owner)

        logo_path = f"logos/{org.pk}/integration-test.jpg"
        saved_path = default_storage.save(logo_path, ContentFile(large_image_bytes))
        org.logo = saved_path
        org.save(update_fields=["logo"])

        try:
            result = generate_thumbnails_task(
                app="events",
                model="organization",
                pk=str(org.pk),
                field="logo",
            )

            assert result is not None
            thumbnail_path = result["logo_thumbnail"]
            assert default_storage.exists(thumbnail_path)

            # Verify thumbnail dimensions
            with default_storage.open(thumbnail_path, "rb") as f:
                with Image.open(f) as img:
                    assert img.width <= 150
                    assert img.height <= 150

            delete_orphaned_thumbnails_task(thumbnail_paths=[thumbnail_path])

            assert not default_storage.exists(thumbnail_path)
        finally:
            if default_storage.exists(saved_path):
                default_storage.delete(saved_path)
            for path in (result or {}).values():
                if default_storage.exists(path):
                    default_storage.delete(path)

    def test_profile_picture_thumbnail_generation(
        self,
        revel_user_factory: RevelUserFactory,
        rgb_image_bytes: bytes,
    ) -> None:
        """Test thumbnail generation for user profile pictures."""
        user = revel_user_factory()

        profile_path = f"protected/profile-pictures/{user.pk}/profile.jpg"
        saved_path = default_storage.save(profile_path, ContentFile(rgb_image_bytes))
        user.profile_picture = saved_path
        user.save(update_fields=["profile_picture"])

        try:
            result = generate_thumbnails_task(
                app="accounts",
                model="reveluser",
                pk=str(user.pk),
                field="profile_picture",
            )

            assert result is not None
            assert "profile_picture_thumbnail" in result
            assert "profile_picture_preview" in result

            user.refresh_from_db()
            assert user.profile_picture_thumbnail
            assert user.profile_picture_preview
        finally:
            if default_storage.exists(saved_path):
                default_storage.delete(saved_path)
            for path in (result or {}).values():
                if default_storage.exists(path):
                    default_storage.delete(path)

    def test_questionnaire_file_thumbnail_generation(
        self,
        revel_user_factory: RevelUserFactory,
        rgb_image_bytes: bytes,
    ) -> None:
        """Test thumbnail generation for questionnaire file uploads."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from questionnaires.models import QuestionnaireFile

        user = revel_user_factory()

        # Create a questionnaire file instance with a file attached
        uploaded_file = SimpleUploadedFile(
            "test-image.jpg",
            rgb_image_bytes,
            content_type="image/jpeg",
        )

        qfile = QuestionnaireFile(
            uploader=user,
            original_filename="test-image.jpg",
            file_hash="abc123unique",
            mime_type="image/jpeg",
            file_size=len(rgb_image_bytes),
        )
        qfile.file.save("test-image.jpg", uploaded_file, save=False)
        qfile.save()

        saved_path = qfile.file.name

        try:
            result = generate_thumbnails_task(
                app="questionnaires",
                model="questionnairefile",
                pk=str(qfile.pk),
                field="file",
            )

            assert result is not None
            assert "thumbnail" in result
            assert "preview" in result

            qfile.refresh_from_db()
            assert qfile.thumbnail
            assert qfile.preview
        finally:
            if default_storage.exists(saved_path):
                default_storage.delete(saved_path)
            for path in (result or {}).values():
                if default_storage.exists(path):
                    default_storage.delete(path)


# =============================================================================
# Tests for safe_save_uploaded_file integration
# =============================================================================


class TestSafeSaveUploadedFileIntegration:
    """Integration tests for safe_save_uploaded_file with thumbnail generation."""

    def test_safe_save_schedules_thumbnail_generation(
        self,
        revel_user_factory: RevelUserFactory,
        rgb_image_bytes: bytes,
    ) -> None:
        """Test that safe_save_uploaded_file schedules thumbnail generation task."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from common.utils import safe_save_uploaded_file

        owner = revel_user_factory()
        org = Organization.objects.create(name="Test Org", owner=owner)

        uploaded_file = SimpleUploadedFile(
            "test-logo.jpg",
            rgb_image_bytes,
            content_type="image/jpeg",
        )

        with patch("common.thumbnails.tasks.generate_thumbnails_task.delay") as mock_task:
            safe_save_uploaded_file(
                instance=org,
                field="logo",
                file=uploaded_file,
                uploader=owner,
            )

            # Verify thumbnail task was scheduled
            mock_task.assert_called_once_with(
                app="events",
                model="organization",
                pk=str(org.pk),
                field="logo",
            )

        # Cleanup
        if org.logo:
            org.logo.delete(save=False)

    def test_safe_save_clears_old_thumbnails(
        self,
        revel_user_factory: RevelUserFactory,
        rgb_image_bytes: bytes,
    ) -> None:
        """Test that safe_save_uploaded_file clears old thumbnails when replacing file."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from common.utils import safe_save_uploaded_file

        owner = revel_user_factory()
        org = Organization.objects.create(name="Test Org", owner=owner)

        # First upload - set initial logo and thumbnail
        initial_logo = f"logos/{org.pk}/initial.jpg"
        saved_logo = default_storage.save(initial_logo, ContentFile(rgb_image_bytes))
        org.logo = saved_logo

        # Manually set a thumbnail path
        initial_thumbnail = f"logos/{org.pk}/initial_thumbnail.jpg"
        saved_thumb = default_storage.save(initial_thumbnail, ContentFile(rgb_image_bytes))
        org.logo_thumbnail = saved_thumb
        org.save(update_fields=["logo", "logo_thumbnail"])

        try:
            # Upload a new file - should clear old thumbnail
            new_file = SimpleUploadedFile(
                "new-logo.jpg",
                rgb_image_bytes,
                content_type="image/jpeg",
            )

            # Mock both tasks to prevent actual execution
            with (
                patch("common.thumbnails.tasks.delete_orphaned_thumbnails_task.delay") as mock_delete,
                patch("common.thumbnails.tasks.generate_thumbnails_task.delay"),
            ):
                safe_save_uploaded_file(
                    instance=org,
                    field="logo",
                    file=new_file,
                    uploader=owner,
                )

                # Verify old thumbnail deletion was scheduled
                mock_delete.assert_called_once()
                call_args = mock_delete.call_args
                assert saved_thumb in call_args.kwargs["thumbnail_paths"]

            # Verify thumbnail field was cleared (before new thumbnail is generated)
            org.refresh_from_db()
            assert not org.logo_thumbnail
        finally:
            # Cleanup
            if org.logo:
                org.logo.delete(save=False)
            if default_storage.exists(saved_logo):
                default_storage.delete(saved_logo)
            if default_storage.exists(saved_thumb):
                default_storage.delete(saved_thumb)
