"""``validate_image_file`` refuses bytes that are not an image, and its messages are translated (#1573)."""

import io
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils.translation import override
from PIL import Image

from common.fields import MAX_IMAGE_SIZE_BYTES, validate_image_file


def _image(fmt: str, size: tuple[int, int] = (40, 30)) -> SimpleUploadedFile:
    buffer = io.BytesIO()
    Image.new("RGB", size, "red").save(buffer, format=fmt)
    return SimpleUploadedFile(f"image.{fmt.lower()}", buffer.getvalue())


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "WEBP", "HEIF"])
def test_every_allowed_format_passes(fmt: str) -> None:
    validate_image_file(_image(fmt))


@pytest.mark.parametrize(
    "content", [b"not an image at all", b"%PDF-1.7\n", b"<svg xmlns='http://www.w3.org/2000/svg'/>"]
)
def test_bytes_that_are_not_an_image_are_refused(content: bytes) -> None:
    with pytest.raises(ValidationError, match="File is not a valid image."):
        validate_image_file(SimpleUploadedFile("logo.png", content))


def test_the_messages_are_translated() -> None:
    with override("de"), pytest.raises(ValidationError) as exc:
        validate_image_file(SimpleUploadedFile("logo.png", b"not an image at all"))

    assert exc.value.messages == ["Die Datei ist kein gültiges Bild."]


def test_an_oversized_file_is_refused_with_its_limit() -> None:
    oversized = SimpleUploadedFile("big.png", b"0" * (MAX_IMAGE_SIZE_BYTES + 1))

    with pytest.raises(ValidationError, match=rf"Image must be under {MAX_IMAGE_SIZE_BYTES // (1024 * 1024)}MB\."):
        validate_image_file(oversized)


def test_too_many_pixels_is_refused_with_the_translated_limit() -> None:
    """The header gives the size: 1001x1000 is over a budget of 1,000,000 pixels. The number stays in the text."""
    with (
        patch("common.fields.MAX_IMAGE_PIXELS", 1_000_000),
        override("de"),
        pytest.raises(ValidationError) as exc,
    ):
        validate_image_file(_image("PNG", size=(1001, 1000)))

    assert exc.value.messages == ["Das Bild muss kleiner als 1 Megapixel sein."]
