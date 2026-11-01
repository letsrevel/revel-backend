"""``requires_ticket`` is create-only in the Event admin (#1295)."""

import typing as t

import pytest
from django.contrib.admin.sites import site

from accounts.models import RevelUser
from events import models

pytestmark = pytest.mark.django_db


def test_requires_ticket_is_read_only_when_editing(rf: t.Any, superuser: RevelUser, public_event: models.Event) -> None:
    request = rf.get("/")
    request.user = superuser
    model_admin = site._registry[models.Event]

    assert "requires_ticket" in model_admin.get_readonly_fields(request, public_event)


def test_requires_ticket_is_editable_when_adding(rf: t.Any, superuser: RevelUser) -> None:
    request = rf.get("/")
    request.user = superuser
    model_admin = site._registry[models.Event]

    assert "requires_ticket" not in model_admin.get_readonly_fields(request, None)
