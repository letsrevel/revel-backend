"""
This conftest.py provides fixtures for the geo app tests.
"""

import secrets
import string
import typing as t
from datetime import datetime, time, timedelta

import faker
import pytest
from django.utils import timezone
from pytest import MonkeyPatch

from accounts.models import RevelUser
from questionnaires.models import Questionnaire


class MockRecord:
    """A mock representation of an IP2Location record."""

    country_short: str = "AT"
    country_long: str = "Austria"
    region: str = "Vienna"
    city: str = "Vienna"
    latitude: float = 48.2082
    longitude: float = 16.3738


class MockIP2Location:
    """A mock IP2Location object."""

    def get_all(self, ip: str) -> MockRecord | None:
        """
        Returns a MockRecord for any given IP, or None if the IP is empty.
        """
        if not ip:
            return None
        return MockRecord()


@pytest.fixture(autouse=True)
def mock_ip2location(monkeypatch: MonkeyPatch) -> None:
    """
    Mocks the IP2Location database lookup to avoid dependency on the .BIN file.
    This fixture is autouse=True, so it will be active for all tests.
    """

    def mock_get_ip2location() -> MockIP2Location:
        return MockIP2Location()

    monkeypatch.setattr("geo.ip2.get_ip2location", mock_get_ip2location)


@pytest.fixture(autouse=True)
def increase_rate_limit(monkeypatch: MonkeyPatch) -> None:
    """Increase the rate limits for AuthThrottle to allow testing."""
    monkeypatch.setattr("common.throttling.AuthThrottle.rate", "1000/min")


@pytest.fixture(autouse=True)
def enable_celery_eager_mode(settings: t.Any) -> None:
    """Enable Celery eager mode for tests so tasks execute synchronously.

    This ensures that Celery tasks run immediately in the same process,
    allowing tests to verify their side effects without async complications.
    """
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True


@pytest.fixture(autouse=True)
def use_locmem_cache(settings: t.Any) -> None:
    """Use in-memory cache for tests to avoid Redis sharing issues in parallel runs.

    This ensures each test worker has its own isolated cache, preventing race
    conditions when running tests in parallel with pytest-xdist.
    """
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "test-cache",
        }
    }


@pytest.fixture(autouse=True)
def use_fast_password_hasher(settings: t.Any) -> None:
    """Use MD5 password hasher for faster user creation in tests.

    The default PBKDF2 hasher is intentionally slow for security, but this
    adds unnecessary overhead in tests. MD5 is ~100x faster.
    """
    settings.PASSWORD_HASHERS = [
        "django.contrib.auth.hashers.MD5PasswordHasher",
    ]


@pytest.fixture
def questionnaire() -> Questionnaire:
    """Provides a basic Questionnaire instance."""
    from questionnaires.models import Questionnaire

    return Questionnaire.objects.create(name="Test Questionnaire", min_score=75)


@pytest.fixture
def user_with_questionnaire_submission(user: RevelUser, questionnaire: Questionnaire) -> RevelUser:
    """Provides a user with a questionnaire submission."""
    from questionnaires.models import QuestionnaireSubmission

    QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)
    return user


class RevelUserFactory:
    """Factory for creating RevelUser instances for testing."""

    fake = faker.Faker()

    def create_user(self, **kwargs: t.Any) -> RevelUser:
        username = kwargs.pop(
            "username", "".join(secrets.choice(string.ascii_lowercase) for _ in range(8)) + "@user.test"
        )
        email = kwargs.pop("email", username + ("@test.com" if "@" not in username else ""))
        password = kwargs.pop("password", "password")
        first_name = kwargs.pop("first_name", self.fake.first_name())
        last_name = kwargs.pop("last_name", self.fake.last_name())
        preferred_name = kwargs.pop("preferred_name", f"{first_name} {last_name}")
        pronouns = kwargs.pop("pronouns", secrets.choice(["they/them", "he/him", "she/her"]))
        return RevelUser.objects.create_user(
            username=username,
            email=email,
            password=password,
            pronouns=pronouns,
            preferred_name=preferred_name,
            first_name=first_name,
            last_name=last_name,
            **kwargs,
        )

    def __call__(self, **kwargs: t.Any) -> RevelUser:
        return self.create_user(**kwargs)


@pytest.fixture
def revel_user_factory() -> RevelUserFactory:
    return RevelUserFactory()


@pytest.fixture
def superuser(revel_user_factory: RevelUserFactory) -> RevelUser:
    """A superuser."""
    return revel_user_factory(is_superuser=True, is_staff=True)


@pytest.fixture
def next_week() -> datetime:
    today = timezone.now()
    same_time_next_week = today + timedelta(days=7)
    noon = time(hour=12, minute=0)
    return timezone.make_aware(
        datetime.combine(same_time_next_week.date(), noon),
        timezone.get_current_timezone(),
    )


@pytest.fixture(autouse=True)
def mock_telegram_send_message(monkeypatch: MonkeyPatch) -> None:
    """Mock telegram.tasks.send_message_task to prevent sending real messages during tests.

    This fixture is autouse=True to ensure no Telegram messages are accidentally sent
    during test runs. The mock returns None and logs that it was called.
    """
    from unittest.mock import MagicMock

    mock_task = MagicMock(return_value=None)
    # Mock both the task function and its .delay() method for Celery calls
    mock_task.delay = MagicMock(return_value=None)
    mock_task.apply_async = MagicMock(return_value=None)

    monkeypatch.setattr("telegram.tasks.send_message_task", mock_task)


@pytest.fixture(autouse=True)
def mock_clamav(monkeypatch: MonkeyPatch) -> None:
    """Mock ClamAV malware scanning to prevent connection errors during tests.

    This fixture is autouse=True to ensure all file upload tests work without
    requiring a running ClamAV daemon. The mock simulates a clean scan (no malware).
    """
    from unittest.mock import MagicMock

    mock_clamd_class = MagicMock()
    mock_clamd_instance = mock_clamd_class.return_value
    # Mock successful ping
    mock_clamd_instance.ping.return_value = True
    # Mock clean scan (no findings = no malware)
    mock_clamd_instance.scan_stream.return_value = None

    monkeypatch.setattr("common.tasks.pyclamd.ClamdNetworkSocket", mock_clamd_class)


@pytest.fixture
def png_bytes() -> bytes:
    """Return valid PNG bytes for a minimal 1x1 image.

    This fixture provides a valid PNG that passes python-magic MIME type detection,
    useful for testing file upload functionality that validates MIME types from content.
    """
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01"  # Width: 1
        b"\x00\x00\x00\x01"  # Height: 1
        b"\x08\x06\x00\x00\x00"  # Bit depth, color type, compression, filter, interlace
        b"\x1f\x15\xc4\x89"
        b"\x00\x00\x00\x0aIDAT"
        b"\x78\x9c\x63\x00\x01\x00\x00\x05\x00\x01"
        b"\x0d\x0a\x2d\xb4"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


# =============================================================================
# Thumbnail test fixtures (shared across thumbnail tests)
# =============================================================================


@pytest.fixture
def rgb_image_bytes() -> bytes:
    """Create a simple RGB image in memory as JPEG bytes."""
    from io import BytesIO

    from PIL import Image

    img = Image.new("RGB", (200, 150), color="blue")
    buffer = BytesIO()
    img.save(buffer, format="JPEG", quality=85)
    buffer.seek(0)
    return buffer.read()


@pytest.fixture
def large_image_bytes() -> bytes:
    """Create a large image that will need resizing."""
    from io import BytesIO

    from PIL import Image

    img = Image.new("RGB", (2000, 1500), color="green")
    buffer = BytesIO()
    img.save(buffer, format="JPEG", quality=85)
    buffer.seek(0)
    return buffer.read()
