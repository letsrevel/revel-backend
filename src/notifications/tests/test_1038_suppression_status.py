"""Notification preferences expose the user's email suppression status (#1038)."""

import typing as t

import pytest
from django.test.client import Client
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from notifications.models import EmailSuppression

pytestmark = pytest.mark.django_db

URL = "/api/notification-preferences"


def _get(user: RevelUser) -> dict[str, t.Any]:
    refresh = RefreshToken.for_user(user)
    client = Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")  # type: ignore[attr-defined]
    response = client.get(URL)
    assert response.status_code == 200
    return t.cast(dict[str, t.Any], response.json())


def _suppress(email: str, reason: EmailSuppression.Reason) -> EmailSuppression:
    return EmailSuppression.objects.create(
        email=email, reason=reason, source=EmailSuppression.Source.PROVIDER, detail="550 5.1.1 mailbox unavailable"
    )


def test_no_suppression_is_null(regular_user: RevelUser) -> None:
    assert _get(regular_user)["email_suppression"] is None


def test_hard_bounce_exposes_reason_and_since_only(regular_user: RevelUser) -> None:
    row = _suppress("regular@example.com", EmailSuppression.Reason.HARD_BOUNCE)

    status = _get(regular_user)["email_suppression"]

    assert set(status) == {"reason", "since"}
    assert status["reason"] == "hard_bounce"
    assert status["since"].startswith(row.updated_at.strftime("%Y-%m-%dT%H:%M:%S"))
    assert "550" not in str(status)


def test_invitation_opt_out_alone_is_not_reported(regular_user: RevelUser) -> None:
    _suppress("regular@example.com", EmailSuppression.Reason.INVITATION_OPT_OUT)

    assert _get(regular_user)["email_suppression"] is None


def test_suppression_matches_normalized_alias(django_user_model: type[RevelUser]) -> None:
    user = django_user_model.objects.create_user(
        username="alias-user", email="Alias.User+events@example.com", password="password"
    )
    _suppress("alias.user@example.com", EmailSuppression.Reason.COMPLAINT)

    assert _get(user)["email_suppression"]["reason"] == "complaint"
