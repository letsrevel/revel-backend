import sys
from io import BytesIO

import piexif
import pytest
from django.core.files.uploadedfile import InMemoryUploadedFile
from PIL import Image

from common.utils import strip_exif


@pytest.fixture
def image_with_exif() -> InMemoryUploadedFile:
    # 1. Create a base image
    image = Image.new("RGB", (10, 10), color="red")

    # 2. Inject EXIF using piexif
    exif_dict = {
        "0th": {
            piexif.ImageIFD.Orientation: 1,
            piexif.ImageIFD.ImageDescription: "Hello world".encode("utf-8"),
        },
        "Exif": {},
        "GPS": {},
        "1st": {},
        "thumbnail": None,
    }

    exif_bytes = piexif.dump(exif_dict)

    # 3. Save image to buffer with EXIF
    buffer = BytesIO()
    image.save(buffer, format="jpeg", exif=exif_bytes)
    buffer.seek(0)

    return InMemoryUploadedFile(
        file=buffer,
        field_name="test",
        name="test.jpg",
        content_type="image/jpeg",
        size=sys.getsizeof(buffer),
        charset=None,
    )


def test_strip_exif_removes_data(image_with_exif: InMemoryUploadedFile) -> None:
    # Confirm EXIF is there
    image = Image.open(image_with_exif)
    exif = image.getexif()
    assert dict(exif), "Image should contain EXIF before stripping"

    # Strip it
    stripped = strip_exif(image_with_exif)
    stripped.seek(0)

    # Confirm EXIF is gone
    image_stripped = Image.open(stripped)
    exif = image_stripped.getexif()
    assert not dict(exif), "EXIF should be stripped from image"
