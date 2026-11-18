import typing as t
import uuid
from io import BytesIO
from pathlib import Path
from unittest import mock

import piexif
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from ninja.errors import HttpError
from PIL import Image, ImageFile, PngImagePlugin

from accounts.models import RevelUser
from common.models import FileUploadAudit
from events import models
from events.schema import AdditionalResourceCreateSchema
from events.service import resource_service

pytestmark = pytest.mark.django_db

FILE_PAYLOAD = AdditionalResourceCreateSchema(name="Doc", resource_type=models.AdditionalResource.ResourceTypes.FILE)
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
HTML_BYTES = b"<!DOCTYPE html><html><head><title>x</title></head><body><script>alert(1)</script></body></html>"


def _create(
    organization: models.Organization, name: str, content: bytes, uploader: RevelUser
) -> models.AdditionalResource:
    return resource_service.create_resource(
        organization,
        FILE_PAYLOAD,
        file=SimpleUploadedFile(name, content),  # type: ignore[arg-type]
        uploader=uploader,
    )


def test_pdf_is_stored_with_pdf_extension(
    organization: models.Organization, organization_owner_user: RevelUser
) -> None:
    resource = _create(organization, "evil name.pdf", PDF_BYTES, organization_owner_user)

    stored = Path(str(resource.file.name))
    assert stored.suffix == ".pdf"
    uuid.UUID(stored.stem)
    assert "evil" not in stored.name


def test_png_named_html_is_stored_as_png(
    organization: models.Organization, organization_owner_user: RevelUser, png_bytes: bytes
) -> None:
    resource = _create(organization, "x.html", png_bytes, organization_owner_user)

    assert str(resource.file.name).endswith(".png")
    assert "x.html" not in str(resource.file.name)


def test_html_content_named_pdf_is_refused(
    organization: models.Organization, organization_owner_user: RevelUser
) -> None:
    with pytest.raises(HttpError) as exc_info:
        _create(organization, "report.pdf", HTML_BYTES, organization_owner_user)

    assert exc_info.value.status_code == 422
    assert not models.AdditionalResource.objects.filter(organization=organization).exists()


def test_file_over_cap_is_refused(
    organization: models.Organization, organization_owner_user: RevelUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resource_service, "MAX_RESOURCE_FILE_SIZE", 10)

    with pytest.raises(HttpError) as exc_info:
        _create(organization, "report.pdf", PDF_BYTES, organization_owner_user)

    assert exc_info.value.status_code == 422
    assert not models.AdditionalResource.objects.filter(organization=organization).exists()


def test_jpeg_exif_is_stripped(organization: models.Organization, organization_owner_user: RevelUser) -> None:
    buffer = BytesIO()
    exif = piexif.dump({"0th": {piexif.ImageIFD.ImageDescription: b"secret"}, "Exif": {}, "GPS": {}, "1st": {}})
    Image.new("RGB", (10, 10), "red").save(buffer, format="jpeg", exif=exif)

    resource = _create(organization, "photo.jpg", buffer.getvalue(), organization_owner_user)

    assert str(resource.file.name).endswith(".jpg")
    with resource.file.open("rb") as stored:
        assert not dict(Image.open(stored).getexif())


def test_multi_page_tiff_keeps_all_pages(organization: models.Organization, organization_owner_user: RevelUser) -> None:
    buffer = BytesIO()
    first = Image.new("RGB", (10, 10), "red")
    first.save(buffer, format="tiff", save_all=True, append_images=[Image.new("RGB", (10, 10), "blue")])

    resource = _create(organization, "scan.tiff", buffer.getvalue(), organization_owner_user)

    with resource.file.open("rb") as stored:
        assert getattr(Image.open(stored), "n_frames", 1) == 2


def test_image_with_oversized_text_chunk_is_refused_with_422(
    organization: models.Organization, organization_owner_user: RevelUser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pillow raises ValueError (not OSError) for this input; it must still be a 422."""
    # weasyprint / pillow_heif switch this on process-wide, which makes Pillow swallow the error.
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", False)
    info = PngImagePlugin.PngInfo()
    info.add_text("k", "x" * (PngImagePlugin.MAX_TEXT_CHUNK + 1), zip=True)
    buffer = BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, format="png", pnginfo=info)

    with pytest.raises(HttpError) as exc_info:
        _create(organization, "chunk.png", buffer.getvalue(), organization_owner_user)

    assert exc_info.value.status_code == 422
    assert not models.AdditionalResource.objects.filter(organization=organization).exists()


def test_audit_and_scan_are_registered(
    organization: models.Organization,
    organization_owner_user: RevelUser,
    settings: t.Any,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """The scan is scheduled via ``on_commit``, so the callbacks must be executed explicitly."""
    settings.FEATURE_MALWARE_SCAN = True
    with mock.patch("common.service.upload_service.tasks.scan_for_malware") as scan:
        with django_capture_on_commit_callbacks(execute=True):
            resource = _create(organization, "report.pdf", PDF_BYTES, organization_owner_user)

    scan.delay.assert_called_once_with(app="events", model="additionalresource", pk=str(resource.pk), field="file")
    audit = FileUploadAudit.objects.get(app="events", model="additionalresource", instance_pk=resource.pk)
    assert audit.uploader == organization_owner_user.email


MALFORMED_WEBP = b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 20


def test_malformed_image_is_refused_with_422(
    organization: models.Organization, organization_owner_user: RevelUser
) -> None:
    with pytest.raises(HttpError) as exc_info:
        _create(organization, "bad.webp", MALFORMED_WEBP, organization_owner_user)

    assert exc_info.value.status_code == 422
    assert not models.AdditionalResource.objects.filter(organization=organization).exists()


def test_oversized_image_is_refused_with_422(
    organization: models.Organization,
    organization_owner_user: RevelUser,
    png_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("common.utils.MAX_IMAGE_PIXELS", 0)

    with pytest.raises(HttpError) as exc_info:
        _create(organization, "big.png", png_bytes, organization_owner_user)

    assert exc_info.value.status_code == 422
    assert not models.AdditionalResource.objects.filter(organization=organization).exists()


def test_missing_name_takes_original_file_name(
    organization: models.Organization, organization_owner_user: RevelUser
) -> None:
    payload = AdditionalResourceCreateSchema(resource_type=models.AdditionalResource.ResourceTypes.FILE)
    long_name = "a" * 300 + ".pdf"

    resource = resource_service.create_resource(
        organization,
        payload,
        file=SimpleUploadedFile("../../etc/report.pdf", PDF_BYTES),  # type: ignore[arg-type]
        uploader=organization_owner_user,
    )
    long_resource = resource_service.create_resource(
        organization,
        payload,
        file=SimpleUploadedFile(long_name, PDF_BYTES),  # type: ignore[arg-type]
        uploader=organization_owner_user,
    )

    assert resource.name == "report.pdf"
    assert long_resource.name and len(long_resource.name) <= 255


def test_given_name_is_kept(organization: models.Organization, organization_owner_user: RevelUser) -> None:
    resource = _create(organization, "report.pdf", PDF_BYTES, organization_owner_user)

    assert resource.name == "Doc"
