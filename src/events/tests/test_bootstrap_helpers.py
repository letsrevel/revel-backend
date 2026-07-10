"""Tests for bootstrap seeding helpers."""

import pytest

from accounts.models import GlobalBan
from events.management.commands.bootstrap_helpers.users import create_global_bans

pytestmark = pytest.mark.django_db


class TestCreateGlobalBans:
    """Coverage for ``create_global_bans``."""

    def test_seeds_email_and_domain_bans(self) -> None:
        create_global_bans()

        assert GlobalBan.objects.filter(ban_type=GlobalBan.BanType.EMAIL, value="banned.user@example.com").exists()
        assert GlobalBan.objects.filter(ban_type=GlobalBan.BanType.DOMAIN, value="banned.example").exists()

    def test_is_idempotent(self) -> None:
        """Regression for issue #665.

        ``reset_events`` re-runs ``bootstrap_events`` without clearing GlobalBan
        rows, so re-seeding previously collided with the ``(ban_type,
        normalized_value)`` uniqueness constraint and raised ``ValidationError``,
        leaving the DB half-wiped. Seeding must be idempotent.
        """
        create_global_bans()
        create_global_bans()  # must not raise

        assert GlobalBan.objects.filter(ban_type=GlobalBan.BanType.EMAIL).count() == 1
        assert GlobalBan.objects.filter(ban_type=GlobalBan.BanType.DOMAIN).count() == 1
