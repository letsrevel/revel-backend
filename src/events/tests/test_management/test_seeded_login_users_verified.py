"""Seeded superusers must be created verified: password login refuses unverified accounts."""

import io
from unittest.mock import patch

import pytest
from django.core.management import call_command

from accounts.models import RevelUser
from events.management.commands.seed import Command as SeedCommand

pytestmark = pytest.mark.django_db


SUPERUSER_EMAIL = "seeded-admin@example.com"


@pytest.fixture(autouse=True)
def _pin_superuser_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The commands read these via decouple (env first), so a local .env cannot change what is queried."""
    monkeypatch.setenv("DEFAULT_SUPERUSER_USERNAME", SUPERUSER_EMAIL)
    monkeypatch.setenv("DEFAULT_SUPERUSER_EMAIL", SUPERUSER_EMAIL)


def test_bootstrap_superuser_is_verified() -> None:
    """``bootstrap`` creates the default superuser verified (events seeding is stubbed out)."""
    real_call_command = call_command

    def fake(name: str, *args: object, **kwargs: object) -> object:
        if name == "bootstrap_events":
            return None
        return real_call_command(name, *args, **kwargs)

    with patch("common.management.commands.bootstrap.call_command", side_effect=fake):
        real_call_command("bootstrap", stdout=io.StringIO())

    user = RevelUser.objects.get(username=SUPERUSER_EMAIL)
    assert user.is_superuser
    assert user.email_verified is True


def test_seed_superuser_is_verified() -> None:
    cmd = SeedCommand(stdout=io.StringIO())
    cmd._create_superuser()

    user = RevelUser.objects.get(username=SUPERUSER_EMAIL)
    assert user.is_superuser
    assert user.email_verified is True
