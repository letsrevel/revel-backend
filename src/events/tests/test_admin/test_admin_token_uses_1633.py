"""The token admins never write back a stale ``uses`` count.

The claim services count ``uses`` with ``F()`` updates (#1633). The change page of a token must not write a
stale count back, so ``uses`` is read-only, a save writes only the form fields, and the edit guard refuses a
save over a value that changed after the page opened.
"""

import typing as t
from datetime import timedelta

import pytest
from django.contrib.admin.sites import site
from django.db.models import F
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import EventToken, OrganizationToken, TicketTier
from events.tests.test_admin.test_admin_edit_guard import STALE, _request, _round_trip
from events.tests.test_admin.test_admin_write_paths_1594_1597 import _save_with_concurrent_write

pytestmark = pytest.mark.django_db


@pytest.fixture
def no_manifest_storage(settings: t.Any) -> None:
    """The change page renders unfold's static files, which have no manifest in tests."""
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture(params=["event_token", "organization_token"])
def token(request: t.Any) -> t.Any:
    """Each token model, one test run per model."""
    obj: EventToken | OrganizationToken = request.getfixturevalue(request.param)
    return obj


def test_uses_is_read_only(rf: t.Any, superuser: RevelUser, token: t.Any) -> None:
    model_admin = site._registry[type(token)]

    readonly = model_admin.get_readonly_fields(_request(rf, superuser), token)

    assert "uses" in readonly


def test_a_concurrent_claim_is_kept(rf: t.Any, superuser: RevelUser, token: t.Any) -> None:
    model = type(token)
    model.objects.filter(pk=token.pk).update(updated_at=timezone.now() - timedelta(days=1))
    token.refresh_from_db()
    before = token.updated_at

    _save_with_concurrent_write(
        rf,
        superuser,
        token,
        {"name": "Renamed"},
        lambda: model.objects.filter(pk=token.pk).update(uses=F("uses") + 1),
    )

    token.refresh_from_db()
    assert token.uses == 1
    assert token.name == "Renamed"
    assert token.updated_at > before


@pytest.mark.usefixtures("no_manifest_storage")
def test_an_unedited_round_trip_saves(client: Client, superuser: RevelUser, token: t.Any) -> None:
    client.force_login(superuser)
    url = reverse(f"admin:events_{type(token)._meta.model_name}_change", args=[token.pk])

    response = _round_trip(client, url)

    assert response.status_code == 302


@pytest.mark.usefixtures("no_manifest_storage")
def test_a_stale_edit_is_refused(client: Client, superuser: RevelUser, token: t.Any) -> None:
    client.force_login(superuser)
    model = type(token)
    url = reverse(f"admin:events_{model._meta.model_name}_change", args=[token.pk])

    response = _round_trip(
        client,
        url,
        edit={"name": "Renamed"},
        before_post=lambda: model.objects.filter(pk=token.pk).update(name="Service"),
    )

    assert response.status_code == 200
    assert STALE in response.content.decode()
    token.refresh_from_db()
    assert token.name == "Service"


@pytest.mark.usefixtures("no_manifest_storage")
def test_an_unedited_round_trip_with_values_saves(
    client: Client, superuser: RevelUser, event_token: EventToken, event_ticket_tier: TicketTier
) -> None:
    """The expiry (split datetime with microseconds), the JSON payload and the tiers all round-trip unchanged."""
    EventToken.objects.filter(pk=event_token.pk).update(
        expires_at=timezone.now() + timedelta(days=3, microseconds=123456),
        invitation_payload={"waives_questionnaire": True},
    )
    event_token.ticket_tiers.add(event_ticket_tier)
    client.force_login(superuser)

    response = _round_trip(client, reverse("admin:events_eventtoken_change", args=[event_token.pk]))

    assert response.status_code == 302
