"""Tests for the ``bootstrap_admin`` first-run management command."""

import io

import pytest
from django.core.management import call_command
from django.test import override_settings

from accounts.models import RevelUser
from events.models import Organization

pytestmark = pytest.mark.django_db


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    inputs: list[str],
    passwords: list[str],
) -> str:
    """Drive the interactive command with canned ``input``/``getpass`` answers."""
    monkeypatch.setattr("builtins.input", lambda _prompt="": inputs.pop(0))
    monkeypatch.setattr(
        "accounts.management.commands.bootstrap_admin.getpass.getpass",
        lambda _prompt="": passwords.pop(0),
    )
    out = io.StringIO()
    call_command("bootstrap_admin", stdout=out, stderr=io.StringIO())
    return out.getvalue()


@override_settings(
    BASE_URL="https://api.example.org",
    ADMIN_URL="admin/",
    FRONTEND_BASE_URL="https://example.org",
)
def test_creates_superuser_and_org_with_chosen_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: superuser (username==email, verified) + org with the chosen slug; URLs printed."""
    output = _run(
        monkeypatch,
        inputs=["admin@example.org", "Acme Collective", "my-acme"],
        passwords=["Sup3rSecret!pw", "Sup3rSecret!pw"],
    )

    user = RevelUser.objects.get(email="admin@example.org")
    assert user.username == "admin@example.org"  # pinned to email, like the public API
    assert user.is_superuser and user.is_staff
    assert user.email_verified is True
    assert user.check_password("Sup3rSecret!pw")

    org = Organization.objects.get(slug="my-acme")
    assert org.owner == user
    assert org.name == "Acme Collective"
    assert org.contact_email == "admin@example.org"
    assert org.contact_email_verified is True  # auto-verified, no mail sent

    assert "https://api.example.org/admin/" in output
    assert "https://example.org" in output


def test_slug_defaults_to_slugified_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pressing Enter at the slug prompt accepts the proposed slugify(name)."""
    _run(
        monkeypatch,
        inputs=["owner@example.org", "Acme Collective", ""],
        passwords=["Sup3rSecret!pw", "Sup3rSecret!pw"],
    )
    assert Organization.objects.filter(slug="acme-collective").exists()


def test_rejects_existing_email_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """An already-taken email is rejected; the prompt loops until a fresh one is given."""
    RevelUser.objects.create_user(username="taken@example.org", email="taken@example.org", password="x")
    _run(
        monkeypatch,
        inputs=["taken@example.org", "fresh@example.org", "Acme Collective", "acme"],
        passwords=["Sup3rSecret!pw", "Sup3rSecret!pw"],
    )
    assert RevelUser.objects.filter(username="fresh@example.org", is_superuser=True).exists()


def test_password_mismatch_then_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mismatched passwords loop until a matching, valid pair is entered."""
    _run(
        monkeypatch,
        inputs=["admin@example.org", "Acme Collective", "acme"],
        passwords=["Sup3rSecret!pw", "different", "Sup3rSecret!pw", "Sup3rSecret!pw"],
    )
    assert RevelUser.objects.get(email="admin@example.org").check_password("Sup3rSecret!pw")


def test_invalid_email_then_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed email is rejected before a valid one is accepted."""
    _run(
        monkeypatch,
        inputs=["not-an-email", "admin@example.org", "Acme Collective", "acme"],
        passwords=["Sup3rSecret!pw", "Sup3rSecret!pw"],
    )
    assert RevelUser.objects.filter(username="admin@example.org").exists()


def test_duplicate_slug_then_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slug already in use is rejected; the prompt loops until a free one is given."""
    existing = RevelUser.objects.create_user(username="other@example.org", email="other@example.org", password="x")
    Organization.objects.create(name="Existing", slug="acme", owner=existing, contact_email="other@example.org")
    _run(
        monkeypatch,
        inputs=["admin@example.org", "Acme Collective", "acme", "acme-two"],
        passwords=["Sup3rSecret!pw", "Sup3rSecret!pw"],
    )
    assert Organization.objects.filter(slug="acme-two").exists()
