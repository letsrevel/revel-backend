"""Tests for profile picture upload and delete endpoints.

This module tests the AccountController endpoints:
- POST /account/me/upload-profile-picture
- DELETE /account/me/delete-profile-picture
"""

from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client
from PIL import Image

from accounts.models import RevelUser
from common.models import FileUploadAudit

pytestmark = pytest.mark.django_db


# --- Helper fixtures ---


@pytest.fixture
def jpg_bytes() -> bytes:
    """Return valid JPEG bytes for testing."""
    img = Image.new("RGB", (100, 100), color="blue")
    buffer = BytesIO()
    img.save(buffer, format="JPEG")
    buffer.seek(0)
    return buffer.read()


@pytest.fixture
def jpg_file(jpg_bytes: bytes) -> SimpleUploadedFile:
    """Return a valid JPEG file upload."""
    return SimpleUploadedFile(
        name="profile.jpg",
        content=jpg_bytes,
        content_type="image/jpeg",
    )


@pytest.fixture
def webp_bytes() -> bytes:
    """Return valid WebP bytes for testing."""
    img = Image.new("RGB", (100, 100), color="green")
    buffer = BytesIO()
    img.save(buffer, format="WEBP")
    buffer.seek(0)
    return buffer.read()


@pytest.fixture
def webp_file(webp_bytes: bytes) -> SimpleUploadedFile:
    """Return a valid WebP file upload."""
    return SimpleUploadedFile(
        name="profile.webp",
        content=webp_bytes,
        content_type="image/webp",
    )


@pytest.fixture
def large_png_bytes() -> bytes:
    """Return a PNG that exceeds the 10MB limit."""
    # Create a large image that will exceed 10MB when saved
    # A 4000x4000 RGBA image is ~64MB uncompressed, and PNG compression
    # should still keep it above 10MB
    img = Image.new("RGBA", (4000, 4000), color="red")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer.read()


@pytest.fixture
def large_file(large_png_bytes: bytes) -> SimpleUploadedFile:
    """Return a file that exceeds the 10MB limit."""
    return SimpleUploadedFile(
        name="large.png",
        content=large_png_bytes,
        content_type="image/png",
    )


@pytest.fixture
def text_file() -> SimpleUploadedFile:
    """Return a text file (invalid image type)."""
    return SimpleUploadedFile(
        name="document.txt",
        content=b"This is a text file, not an image.",
        content_type="text/plain",
    )


@pytest.fixture
def pdf_file() -> SimpleUploadedFile:
    """Return a PDF file (invalid image type)."""
    # Minimal PDF content
    pdf_content = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [] /Count 0 >>
endobj
xref
0 3
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
trailer
<< /Size 3 /Root 1 0 R >>
startxref
109
%%EOF"""
    return SimpleUploadedFile(
        name="document.pdf",
        content=pdf_content,
        content_type="application/pdf",
    )


@pytest.fixture
def fake_png_file() -> SimpleUploadedFile:
    """Return a file with .png extension but invalid image content."""
    return SimpleUploadedFile(
        name="fake.png",
        content=b"This is not actually a PNG file!",
        content_type="image/png",
    )


@pytest.fixture
def valid_image_with_disallowed_extension(png_bytes: bytes) -> SimpleUploadedFile:
    """Return a valid PNG image but with disallowed .exe extension."""
    return SimpleUploadedFile(
        name="image.exe",
        content=png_bytes,
        content_type="image/png",
    )


# --- Tests for POST /account/me/upload-profile-picture ---


class TestUploadProfilePicture:
    """Tests for the profile picture upload endpoint."""

    def test_upload_valid_png_returns_200_with_user_data(
        self,
        auth_client: Client,
        user: RevelUser,
        png_bytes: bytes,
    ) -> None:
        """Test that uploading a valid PNG image returns 200 with updated user schema.

        This verifies the happy path where a user uploads a valid image file,
        and the response includes the profile_picture_url in the schema.
        """
        # Arrange
        png_file = SimpleUploadedFile(
            name="profile.png",
            content=png_bytes,
            content_type="image/png",
        )
        url = reverse("api:upload-profile-picture")

        # Act
        response = auth_client.post(url, data={"profile_picture": png_file}, format="multipart")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(user.pk)
        assert data["email"] == user.email
        # Profile picture URL should be set (signed URL)
        assert "profile_picture_url" in data
        assert data["profile_picture_url"] is not None
        # Verify the file was actually saved
        user.refresh_from_db()
        assert user.profile_picture
        assert user.profile_picture.name.endswith(".png")

    def test_upload_valid_jpeg_returns_200(
        self,
        auth_client: Client,
        user: RevelUser,
        jpg_file: SimpleUploadedFile,
    ) -> None:
        """Test that uploading a valid JPEG image is accepted."""
        # Arrange
        url = reverse("api:upload-profile-picture")

        # Act
        response = auth_client.post(url, data={"profile_picture": jpg_file}, format="multipart")

        # Assert
        assert response.status_code == 200
        user.refresh_from_db()
        assert user.profile_picture
        assert "profile" in user.profile_picture.name.lower()

    def test_upload_valid_webp_returns_200(
        self,
        auth_client: Client,
        user: RevelUser,
        webp_file: SimpleUploadedFile,
    ) -> None:
        """Test that uploading a valid WebP image is accepted."""
        # Arrange
        url = reverse("api:upload-profile-picture")

        # Act
        response = auth_client.post(url, data={"profile_picture": webp_file}, format="multipart")

        # Assert
        assert response.status_code == 200
        user.refresh_from_db()
        assert user.profile_picture

    def test_upload_creates_file_upload_audit(
        self,
        auth_client: Client,
        user: RevelUser,
        png_bytes: bytes,
    ) -> None:
        """Test that uploading creates a FileUploadAudit record for malware scanning.

        The system should create an audit record to track uploaded files and
        schedule a malware scan via the ClamAV integration.
        Note: In tests, Celery runs eagerly so the status will be CLEAN after the scan completes.
        """
        # Arrange
        png_file = SimpleUploadedFile(
            name="profile.png",
            content=png_bytes,
            content_type="image/png",
        )
        url = reverse("api:upload-profile-picture")
        initial_audit_count = FileUploadAudit.objects.count()

        # Act
        response = auth_client.post(url, data={"profile_picture": png_file}, format="multipart")

        # Assert
        assert response.status_code == 200
        assert FileUploadAudit.objects.count() == initial_audit_count + 1
        audit = FileUploadAudit.objects.filter(
            app="accounts",
            model="reveluser",
            instance_pk=user.pk,
            field="profile_picture",
        ).first()
        assert audit is not None
        assert audit.uploader == user.email
        # In tests, Celery runs eagerly so the scan completes immediately
        assert audit.status == FileUploadAudit.FileUploadAuditStatus.CLEAN

    def test_upload_replaces_existing_profile_picture(
        self,
        auth_client: Client,
        user: RevelUser,
        png_bytes: bytes,
        jpg_bytes: bytes,
    ) -> None:
        """Test that uploading a new picture replaces the old one.

        When a user already has a profile picture and uploads a new one,
        the old file should be deleted and replaced with the new one.
        """
        # Arrange
        url = reverse("api:upload-profile-picture")
        # Upload first image
        first_file = SimpleUploadedFile(
            name="first.png",
            content=png_bytes,
            content_type="image/png",
        )
        auth_client.post(url, data={"profile_picture": first_file}, format="multipart")
        user.refresh_from_db()
        old_picture_name = user.profile_picture.name

        # Act - Upload second image
        second_file = SimpleUploadedFile(
            name="second.jpg",
            content=jpg_bytes,
            content_type="image/jpeg",
        )
        response = auth_client.post(url, data={"profile_picture": second_file}, format="multipart")

        # Assert
        assert response.status_code == 200
        user.refresh_from_db()
        assert user.profile_picture.name != old_picture_name
        assert "second" in user.profile_picture.name.lower() or user.profile_picture.name.endswith(".jpg")

    def test_upload_too_large_returns_400(
        self,
        auth_client: Client,
        user: RevelUser,
    ) -> None:
        """Test that uploading a file exceeding 10MB is rejected with 400.

        The system validates file size before saving and returns a proper
        validation error for files larger than the 10MB limit.
        """
        # Arrange - Create a file that's larger than 10MB (invalid content, but size matters)
        large_content = b"x" * (11 * 1024 * 1024)
        large_file = SimpleUploadedFile(
            name="huge.png",
            content=large_content,
            content_type="image/png",
        )
        url = reverse("api:upload-profile-picture")

        # Act
        response = auth_client.post(url, data={"profile_picture": large_file}, format="multipart")

        # Assert - 400 validation error (validators run before save)
        assert response.status_code == 400
        data = response.json()
        assert "errors" in data
        assert "profile_picture" in data["errors"]
        # Verify no profile picture was saved
        user.refresh_from_db()
        assert not user.profile_picture

    def test_upload_invalid_extension_returns_400(
        self,
        auth_client: Client,
        user: RevelUser,
        text_file: SimpleUploadedFile,
    ) -> None:
        """Test that uploading a file with invalid extension is rejected with 400.

        Only image extensions (jpg, jpeg, png, gif, webp) are allowed.
        Text files are rejected with a validation error.
        """
        # Arrange
        url = reverse("api:upload-profile-picture")

        # Act
        response = auth_client.post(url, data={"profile_picture": text_file}, format="multipart")

        # Assert - 400 validation error for invalid extension
        assert response.status_code == 400
        data = response.json()
        assert "errors" in data
        assert "profile_picture" in data["errors"]
        user.refresh_from_db()
        assert not user.profile_picture

    def test_upload_pdf_returns_400(
        self,
        auth_client: Client,
        user: RevelUser,
        pdf_file: SimpleUploadedFile,
    ) -> None:
        """Test that uploading a PDF file is rejected with 400.

        PDF files are not valid image formats and are rejected
        with a validation error (invalid extension).
        """
        # Arrange
        url = reverse("api:upload-profile-picture")

        # Act
        response = auth_client.post(url, data={"profile_picture": pdf_file}, format="multipart")

        # Assert - 400 validation error for invalid extension
        assert response.status_code == 400
        data = response.json()
        assert "errors" in data
        assert "profile_picture" in data["errors"]
        user.refresh_from_db()
        assert not user.profile_picture

    def test_upload_fake_image_returns_400(
        self,
        auth_client: Client,
        user: RevelUser,
        fake_png_file: SimpleUploadedFile,
    ) -> None:
        """Test that files with image extension but invalid content are rejected.

        The system validates actual image content, not just extension.
        Files with fake image extensions (e.g., text content with .png extension)
        are rejected with a proper validation error.
        """
        # Arrange
        url = reverse("api:upload-profile-picture")

        # Act
        response = auth_client.post(url, data={"profile_picture": fake_png_file}, format="multipart")

        # Assert - 400 validation error (invalid image content)
        assert response.status_code == 400
        data = response.json()
        assert "errors" in data
        assert "profile_picture" in data["errors"]
        user.refresh_from_db()
        assert not user.profile_picture

    def test_upload_disallowed_extension_returns_400(
        self,
        auth_client: Client,
        user: RevelUser,
        valid_image_with_disallowed_extension: SimpleUploadedFile,
    ) -> None:
        """Test that files with disallowed extensions are rejected.

        Even if the file content is a valid image, files with extensions
        not in the allowed list (like .exe) are rejected with a validation error.
        """
        # Arrange
        url = reverse("api:upload-profile-picture")

        # Act
        response = auth_client.post(
            url, data={"profile_picture": valid_image_with_disallowed_extension}, format="multipart"
        )

        # Assert - 400 validation error for disallowed extension
        assert response.status_code == 400
        data = response.json()
        assert "errors" in data
        assert "profile_picture" in data["errors"]
        user.refresh_from_db()
        assert not user.profile_picture

    def test_upload_without_authentication_returns_401(
        self,
        client: Client,
        png_bytes: bytes,
    ) -> None:
        """Test that unauthenticated requests are rejected with 401.

        The upload endpoint requires JWT authentication; requests without
        a valid token should receive an unauthorized response.
        """
        # Arrange
        png_file = SimpleUploadedFile(
            name="profile.png",
            content=png_bytes,
            content_type="image/png",
        )
        url = reverse("api:upload-profile-picture")

        # Act
        response = client.post(url, data={"profile_picture": png_file}, format="multipart")

        # Assert
        assert response.status_code == 401

    def test_upload_stores_file_in_correct_path(
        self,
        auth_client: Client,
        user: RevelUser,
        png_bytes: bytes,
    ) -> None:
        """Test that uploaded files are stored in the expected path structure.

        Profile pictures should be stored in:
        protected/profile-pictures/{user_id}/{filename}
        """
        # Arrange
        png_file = SimpleUploadedFile(
            name="profile.png",
            content=png_bytes,
            content_type="image/png",
        )
        url = reverse("api:upload-profile-picture")

        # Act
        response = auth_client.post(url, data={"profile_picture": png_file}, format="multipart")

        # Assert
        assert response.status_code == 200
        user.refresh_from_db()
        # Check the path contains user ID and is in protected directory
        assert str(user.pk) in user.profile_picture.name
        assert "profile-pictures" in user.profile_picture.name


# --- Tests for DELETE /account/me/delete-profile-picture ---


class TestDeleteProfilePicture:
    """Tests for the profile picture delete endpoint."""

    def test_delete_existing_picture_returns_204(
        self,
        auth_client: Client,
        user: RevelUser,
        png_bytes: bytes,
    ) -> None:
        """Test that deleting an existing profile picture returns 204.

        When a user has a profile picture and deletes it, the response
        should be 204 No Content and the file should be removed.
        """
        # Arrange - First upload a picture
        png_file = SimpleUploadedFile(
            name="profile.png",
            content=png_bytes,
            content_type="image/png",
        )
        upload_url = reverse("api:upload-profile-picture")
        auth_client.post(upload_url, data={"profile_picture": png_file}, format="multipart")
        user.refresh_from_db()
        assert user.profile_picture  # Verify upload succeeded

        # Act
        delete_url = reverse("api:delete-profile-picture")
        response = auth_client.delete(delete_url)

        # Assert
        assert response.status_code == 204
        user.refresh_from_db()
        assert not user.profile_picture

    def test_delete_when_no_picture_exists_returns_204(
        self,
        auth_client: Client,
        user: RevelUser,
    ) -> None:
        """Test that deleting when no picture exists is idempotent.

        The delete operation should be idempotent - calling it when no
        profile picture exists should still return 204, not an error.
        """
        # Arrange - Ensure no profile picture exists
        assert not user.profile_picture

        # Act
        url = reverse("api:delete-profile-picture")
        response = auth_client.delete(url)

        # Assert
        assert response.status_code == 204

    def test_delete_without_authentication_returns_401(
        self,
        client: Client,
    ) -> None:
        """Test that unauthenticated delete requests are rejected.

        The delete endpoint requires JWT authentication; requests without
        a valid token should receive an unauthorized response.
        """
        # Arrange
        url = reverse("api:delete-profile-picture")

        # Act
        response = client.delete(url)

        # Assert
        assert response.status_code == 401

    def test_delete_removes_file_from_storage(
        self,
        auth_client: Client,
        user: RevelUser,
        png_bytes: bytes,
    ) -> None:
        """Test that deleting actually removes the file from storage.

        The delete operation should not only clear the database field
        but also remove the actual file from the storage backend.
        """
        # Arrange - Upload a picture first
        png_file = SimpleUploadedFile(
            name="profile.png",
            content=png_bytes,
            content_type="image/png",
        )
        upload_url = reverse("api:upload-profile-picture")
        auth_client.post(upload_url, data={"profile_picture": png_file}, format="multipart")
        user.refresh_from_db()
        file_name = user.profile_picture.name
        storage = user.profile_picture.storage

        # Act
        delete_url = reverse("api:delete-profile-picture")
        response = auth_client.delete(delete_url)

        # Assert
        assert response.status_code == 204
        # Verify file is removed from storage
        assert not storage.exists(file_name)

    def test_delete_twice_is_idempotent(
        self,
        auth_client: Client,
        user: RevelUser,
        png_bytes: bytes,
    ) -> None:
        """Test that calling delete multiple times is safe and idempotent.

        Users might accidentally click delete multiple times or retry
        on network errors. The endpoint should handle this gracefully.
        """
        # Arrange - Upload a picture
        png_file = SimpleUploadedFile(
            name="profile.png",
            content=png_bytes,
            content_type="image/png",
        )
        upload_url = reverse("api:upload-profile-picture")
        auth_client.post(upload_url, data={"profile_picture": png_file}, format="multipart")
        delete_url = reverse("api:delete-profile-picture")

        # Act - Delete twice
        response1 = auth_client.delete(delete_url)
        response2 = auth_client.delete(delete_url)

        # Assert
        assert response1.status_code == 204
        assert response2.status_code == 204
        user.refresh_from_db()
        assert not user.profile_picture


# --- Integration Tests ---


class TestProfilePictureIntegration:
    """Integration tests for the complete profile picture workflow."""

    def test_full_upload_delete_upload_cycle(
        self,
        auth_client: Client,
        user: RevelUser,
        png_bytes: bytes,
        jpg_bytes: bytes,
    ) -> None:
        """Test a complete cycle of upload -> delete -> upload again.

        This tests the full lifecycle of profile picture management
        to ensure there are no state-related issues.
        """
        # Arrange
        upload_url = reverse("api:upload-profile-picture")
        delete_url = reverse("api:delete-profile-picture")

        # Act & Assert - First upload
        first_file = SimpleUploadedFile("first.png", png_bytes, content_type="image/png")
        response1 = auth_client.post(upload_url, data={"profile_picture": first_file}, format="multipart")
        assert response1.status_code == 200
        user.refresh_from_db()
        assert user.profile_picture

        # Delete
        response2 = auth_client.delete(delete_url)
        assert response2.status_code == 204
        user.refresh_from_db()
        assert not user.profile_picture

        # Second upload with different format
        second_file = SimpleUploadedFile("second.jpg", jpg_bytes, content_type="image/jpeg")
        response3 = auth_client.post(upload_url, data={"profile_picture": second_file}, format="multipart")
        assert response3.status_code == 200
        user.refresh_from_db()
        assert user.profile_picture

    def test_me_endpoint_includes_profile_picture_url(
        self,
        auth_client: Client,
        user: RevelUser,
        png_bytes: bytes,
    ) -> None:
        """Test that the /me endpoint returns profile_picture_url after upload.

        After uploading a profile picture, the GET /account/me endpoint
        should include the signed URL for the profile picture.
        """
        # Arrange - Upload a profile picture
        png_file = SimpleUploadedFile("profile.png", png_bytes, content_type="image/png")
        upload_url = reverse("api:upload-profile-picture")
        auth_client.post(upload_url, data={"profile_picture": png_file}, format="multipart")

        # Act - Get user profile
        me_url = reverse("api:me")
        response = auth_client.get(me_url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "profile_picture_url" in data
        assert data["profile_picture_url"] is not None
        # The URL should be a signed URL (contains signature or token)
        assert len(data["profile_picture_url"]) > 0

    def test_me_endpoint_returns_null_when_no_picture(
        self,
        auth_client: Client,
        user: RevelUser,
    ) -> None:
        """Test that /me returns null profile_picture_url when no picture exists.

        When a user has no profile picture, the profile_picture_url
        should be null, not an empty string or error.
        """
        # Arrange - Ensure no profile picture
        assert not user.profile_picture

        # Act
        url = reverse("api:me")
        response = auth_client.get(url)

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["profile_picture_url"] is None
