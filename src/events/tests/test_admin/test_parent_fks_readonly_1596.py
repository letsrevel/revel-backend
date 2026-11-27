"""The parent of a product on sale is create-only in the Django admin (#1596).

A checkout reads the parent in two steps (reserve, then session or ticket issue). Moving the product between them
would pay or issue against the new parent with the old fee split, so the admin cannot move a series pass to another
series, a series to another organization, or a tier to another event.
"""

import typing as t
from decimal import Decimal

import pytest
from django.contrib.admin.sites import site
from django.contrib.auth.models import Permission
from django.db import models as dj_models
from django.test import Client
from django.urls import reverse

from accounts.models import RevelUser
from events import models

pytestmark = pytest.mark.django_db


@pytest.fixture
def series_pass(event_series: models.EventSeries) -> models.SeriesPass:
    return models.SeriesPass.objects.create(
        event_series=event_series, name="Season", price=Decimal("50.00"), pro_rata_discount=Decimal("5.00")
    )


@pytest.fixture
def tier(public_event: models.Event) -> models.TicketTier:
    return models.TicketTier.objects.create(event=public_event, name="General", price=Decimal("10.00"))


CASES = [
    pytest.param(models.SeriesPass, "series_pass", "event_series", id="series-pass-series"),
    pytest.param(models.EventSeries, "event_series", "organization", id="series-organization"),
    pytest.param(models.TicketTier, "tier", "event", id="tier-event"),
]


def _request(rf: t.Any, superuser: RevelUser) -> t.Any:
    request = rf.get("/")
    request.user = superuser
    return request


@pytest.mark.parametrize(("model", "fixture", "field"), CASES)
def test_the_parent_is_disabled_on_change(
    rf: t.Any,
    superuser: RevelUser,
    request: pytest.FixtureRequest,
    model: type[dj_models.Model],
    fixture: str,
    field: str,
) -> None:
    """A disabled field keeps the stored value, whatever the POST sends."""
    obj = request.getfixturevalue(fixture)
    form_class = site._registry[model].get_form(_request(rf, superuser), obj, change=True)

    assert form_class.base_fields[field].disabled


@pytest.mark.parametrize(("model", "fixture", "field"), CASES)
def test_the_parent_is_editable_on_add(
    rf: t.Any, superuser: RevelUser, model: type[dj_models.Model], fixture: str, field: str
) -> None:
    form_class = site._registry[model].get_form(_request(rf, superuser), None)

    assert not form_class.base_fields[field].disabled


def test_a_posted_parent_is_ignored(
    rf: t.Any, superuser: RevelUser, tier: models.TicketTier, organization: models.Organization
) -> None:
    other_event = models.Event.objects.create(
        organization=organization, name="Other", slug="other", start=tier.event.start, requires_ticket=True
    )
    form_class = site._registry[models.TicketTier].get_form(_request(rf, superuser), tier, change=True)
    data = {**_initial(form_class, tier), "event": str(other_event.pk), "name": "Renamed"}

    form = form_class(data=data, instance=tier)

    assert form.is_valid(), form.errors
    assert form.cleaned_data["event"] == tier.event


def test_a_duplicate_name_is_a_form_error(rf: t.Any, superuser: RevelUser, tier: models.TicketTier) -> None:
    """The unique constraint (event, name) is still checked on the form, not only in ``save()``."""
    models.TicketTier.objects.create(event=tier.event, name="Taken", price=Decimal("10.00"))
    form_class = site._registry[models.TicketTier].get_form(_request(rf, superuser), tier, change=True)
    data = {**_initial(form_class, tier), "name": "Taken"}

    form = form_class(data=data, instance=tier)

    assert not form.is_valid()
    assert "__all__" in form.errors


def _initial(form_class: t.Any, obj: dj_models.Model) -> dict[str, t.Any]:
    """The POST data of an unchanged change form."""
    form = form_class(instance=obj)
    data: dict[str, t.Any] = {}
    for name, field in form.fields.items():
        value = form.initial.get(name, field.initial)
        if value is None:
            continue
        data[name] = (
            [str(v.pk if hasattr(v, "pk") else v) for v in value]
            if isinstance(value, (list, tuple))
            else (str(value.pk) if hasattr(value, "pk") else value)
        )
    return data


@pytest.fixture
def no_manifest_storage(settings: t.Any) -> None:
    """The change page renders unfold's static files, which have no manifest in tests."""
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.mark.usefixtures("no_manifest_storage")
@pytest.mark.parametrize(("model", "fixture", "field"), CASES)
def test_a_view_only_staff_user_can_open_the_change_page(
    client: Client,
    revel_user_factory: t.Any,
    request: pytest.FixtureRequest,
    model: type[dj_models.Model],
    fixture: str,
    field: str,
) -> None:
    """Without change permission the admin drops every field from the form; the mixin must not expect one."""
    obj = request.getfixturevalue(fixture)
    staff = revel_user_factory(is_staff=True)
    opts = model._meta
    staff.user_permissions.add(
        Permission.objects.get(content_type__app_label=opts.app_label, codename=f"view_{opts.model_name}")
    )
    client.force_login(staff)

    response = client.get(reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[obj.pk]))

    assert response.status_code == 200
