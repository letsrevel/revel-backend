"""``viewer_from_context`` must never raise, whatever ninja hands it (#792)."""

import types
import typing as t

import pytest
from django.contrib.auth.models import AnonymousUser

from accounts.models import RevelUser
from common.schema import viewer_from_context


def test_missing_context_is_anonymous() -> None:
    """Schemas built outside a request (``from_orm`` in services) get no context."""
    assert viewer_from_context(None).is_anonymous is True


def test_context_without_request_is_anonymous() -> None:
    assert viewer_from_context({}).is_anonymous is True


def test_request_without_user_is_anonymous() -> None:
    context: dict[str, t.Any] = {"request": types.SimpleNamespace()}

    assert viewer_from_context(context).is_anonymous is True


def test_anonymous_request_user_is_returned() -> None:
    anon = AnonymousUser()
    context: dict[str, t.Any] = {"request": types.SimpleNamespace(user=anon)}

    assert viewer_from_context(context) is anon


@pytest.mark.django_db
def test_authenticated_user_is_returned(django_user_model: type[RevelUser]) -> None:
    user = django_user_model.objects.create_user(
        username="viewer@example.com", email="viewer@example.com", password="pass"
    )
    context: dict[str, t.Any] = {"request": types.SimpleNamespace(user=user)}

    assert viewer_from_context(context) == user
