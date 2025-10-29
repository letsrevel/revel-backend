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
