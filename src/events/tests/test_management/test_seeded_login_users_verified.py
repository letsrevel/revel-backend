"""Seeded superusers must be created verified: password login refuses unverified accounts."""

import io
from unittest.mock import patch

import pytest
from django.core.management import call_command

from accounts.models import RevelUser
from events.management.commands.seed import Command as SeedCommand

pytestmark = pytest.mark.django_db


def test_bootstrap_superuser_is_verified() -> None:
    """``bootstrap`` creates the default superuser verified (events seeding is stubbed out)."""
    real_call_command = call_command

    def fake(name: str, *args: object, **kwargs: object) -> object:
        if name == "bootstrap_events":
            return None
        return real_call_command(name, *args, **kwargs)

    with patch("common.management.commands.bootstrap.call_command", side_effect=fake):
        real_call_command("bootstrap", stdout=io.StringIO())

    user = RevelUser.objects.get(username="admin@letsrevel.io")
    assert user.is_superuser
    assert user.email_verified is True


def test_seed_superuser_is_verified() -> None:
    cmd = SeedCommand(stdout=io.StringIO())
    cmd._create_superuser()

    user = RevelUser.objects.get(username="admin@revel.io")
    assert user.is_superuser
    assert user.email_verified is True
