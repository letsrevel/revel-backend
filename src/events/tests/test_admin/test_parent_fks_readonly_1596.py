"""The parent of a product on sale is create-only in the Django admin (#1596).

A checkout reads the parent in two steps (reserve, then session or ticket issue). Moving the product between them
would pay or issue against the new parent with the old fee split, so the admin cannot move a series pass to another
series, a series to another organization, or a tier to another event.
"""

import typing as t
from decimal import Decimal

import pytest
from django.contrib.admin.sites import site
from django.db import models as dj_models

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
def test_the_parent_is_read_only_on_change(
    rf: t.Any,
    superuser: RevelUser,
    request: pytest.FixtureRequest,
    model: type[dj_models.Model],
    fixture: str,
    field: str,
) -> None:
    obj = request.getfixturevalue(fixture)
    model_admin = site._registry[model]
    admin_request = _request(rf, superuser)

    assert field in model_admin.get_readonly_fields(admin_request, obj)
    assert field not in model_admin.get_form(admin_request, obj).base_fields


@pytest.mark.parametrize(("model", "fixture", "field"), CASES)
def test_the_parent_is_editable_on_add(
    rf: t.Any, superuser: RevelUser, model: type[dj_models.Model], fixture: str, field: str
) -> None:
    model_admin = site._registry[model]
    admin_request = _request(rf, superuser)

    assert field not in model_admin.get_readonly_fields(admin_request, None)
    assert field in model_admin.get_form(admin_request, None).base_fields
