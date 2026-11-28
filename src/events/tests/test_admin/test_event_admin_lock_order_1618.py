"""The event admin change view locks the passes, the tiers and the event before it validates the submit (#1618).

The submit writes the event and then its tier inlines. A new series pass link locks the pass, then the tier, then the
event. Without the early lock, the two could deadlock.
"""

import typing as t
from decimal import Decimal

import pytest
from django.contrib.auth.models import Permission
from django.db import connection
from django.db.models.fields.files import FieldFile
from django.forms import MultiWidget
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Event, EventSeries, SeriesPass, TicketTier
from events.tests.test_series_pass.covered_events import cover

pytestmark = pytest.mark.django_db


@pytest.fixture
def series_pass(event_series: EventSeries) -> SeriesPass:
    return SeriesPass.objects.create(
        event_series=event_series,
        name="Season",
        price=Decimal("30.00"),
        pro_rata_discount=Decimal("5.00"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


@pytest.fixture
def no_manifest_storage(settings: t.Any) -> None:
    """The change page renders unfold's static files, which have no manifest in tests."""
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


def _first(sqls: list[str], table: str) -> int:
    return next(i for i, sql in enumerate(sqls) if "FOR NO KEY UPDATE" in sql and f'FROM "{table}"' in sql)


@pytest.mark.usefixtures("no_manifest_storage")
def test_a_change_submit_locks_pass_then_tiers_then_event_first(
    client: Client, superuser: RevelUser, series_pass: SeriesPass
) -> None:
    event, _tier = cover(series_pass, "admin-lock", days=4)
    client.force_login(superuser)

    with CaptureQueriesContext(connection) as ctx:
        # An empty submit is invalid, so nothing is written: the locks come before the validation.
        response = client.post(reverse("admin:events_event_change", args=[event.pk]), data={})

    assert response.status_code == 200
    sqls = [q["sql"] for q in ctx.captured_queries]
    assert _first(sqls, "events_seriespass") < _first(sqls, "events_tickettier") < _first(sqls, "events_event")
    first_lock = _first(sqls, "events_seriespass")
    assert not any(sql.startswith(("UPDATE", "INSERT")) for sql in sqls[:first_lock])


@pytest.mark.usefixtures("no_manifest_storage")
def test_a_get_takes_no_lock(client: Client, superuser: RevelUser, series_pass: SeriesPass) -> None:
    event, _tier = cover(series_pass, "admin-get", days=4)
    client.force_login(superuser)

    with CaptureQueriesContext(connection) as ctx:
        client.get(reverse("admin:events_event_change", args=[event.pk]))

    assert not any("FOR NO KEY UPDATE" in q["sql"] and "events_tickettier" in q["sql"] for q in ctx.captured_queries)


def _locks(ctx: CaptureQueriesContext) -> list[str]:
    return [q["sql"] for q in ctx.captured_queries if "FOR NO KEY UPDATE" in q["sql"]]


def _post_data_of(form: t.Any, data: dict[str, t.Any]) -> None:
    """Add the POST values of an unchanged bound form, as the browser would send them.

    A field with ``show_hidden_initial`` also sends its hidden ``initial-<name>`` input (the stale check, #1635).
    """
    for bound in form:
        value = bound.value()
        if value is None or value is False or isinstance(value, FieldFile):
            continue  # an image field without an upload sends nothing
        _put(data, bound.html_name, bound.field.widget, value)
        if bound.field.show_hidden_initial:
            _put(data, bound.html_initial_name, bound.field.widget, value)


def _put(data: dict[str, t.Any], name: str, widget: t.Any, value: t.Any) -> None:
    if isinstance(widget, MultiWidget):
        for i, part in enumerate(widget.decompress(value) or ()):
            if part is not None:
                data[f"{name}_{i}"] = part
    elif isinstance(value, (list, tuple)):
        data[name] = [getattr(v, "pk", v) for v in value]
    else:
        data[name] = getattr(value, "pk", value)


def _change_post_data(client: Client, event: Event) -> dict[str, t.Any]:
    ctx = client.get(reverse("admin:events_event_change", args=[event.pk])).context
    data: dict[str, t.Any] = {}
    _post_data_of(ctx["adminform"].form, data)
    for inline in ctx["inline_admin_formsets"]:
        formset = inline.formset
        forms = list(formset.initial_forms)
        mgmt = formset.management_form
        data.update(
            {
                mgmt["TOTAL_FORMS"].html_name: len(forms),
                mgmt["INITIAL_FORMS"].html_name: len(forms),
                mgmt["MIN_NUM_FORMS"].html_name: 0,
                mgmt["MAX_NUM_FORMS"].html_name: 1000,
            }
        )
        for form in forms:
            _post_data_of(form, data)
    return data


@pytest.mark.usefixtures("no_manifest_storage")
def test_a_valid_submit_with_a_tier_inline_edit_locks_before_its_first_write(
    client: Client, superuser: RevelUser, series_pass: SeriesPass
) -> None:
    event, tier = cover(series_pass, "admin-valid", days=4)
    client.force_login(superuser)
    data = _change_post_data(client, event)
    tier_name_key = next(k for k, v in data.items() if v == tier.name and k.endswith("-name"))
    data[tier_name_key] = "Renamed tier"

    with CaptureQueriesContext(connection) as ctx:
        response = client.post(reverse("admin:events_event_change", args=[event.pk]), data)

    assert response.status_code == 302, response.context and [
        response.context["adminform"].form.errors,
        [f.errors for i in response.context["inline_admin_formsets"] for f in i.formset.forms],
    ]
    assert TicketTier.objects.get(pk=tier.pk).name == "Renamed tier"
    sqls = [q["sql"] for q in ctx.captured_queries]
    first_write = next(i for i, sql in enumerate(sqls) if sql.startswith(("UPDATE", "INSERT")))
    assert _first(sqls, "events_seriespass") < _first(sqls, "events_tickettier") < _first(sqls, "events_event")
    assert _first(sqls, "events_event") < first_write


@pytest.mark.usefixtures("no_manifest_storage")
def test_a_staff_user_without_change_permission_takes_no_lock(
    client: Client, revel_user_factory: t.Any, series_pass: SeriesPass
) -> None:
    event, _tier = cover(series_pass, "admin-view-only", days=4)
    staff = revel_user_factory(is_staff=True)
    staff.user_permissions.add(Permission.objects.get(content_type__app_label="events", codename="view_event"))
    client.force_login(staff)

    with CaptureQueriesContext(connection) as ctx:
        response = client.post(reverse("admin:events_event_change", args=[event.pk]), data={})

    assert response.status_code == 403
    assert _locks(ctx) == []


@pytest.mark.usefixtures("no_manifest_storage")
def test_an_unknown_event_takes_no_lock(client: Client, superuser: RevelUser) -> None:
    client.force_login(superuser)

    with CaptureQueriesContext(connection) as ctx:
        response = client.post(
            reverse("admin:events_event_change", args=["00000000-0000-0000-0000-000000000000"]), data={}
        )

    assert response.status_code == 302  # the admin redirects to the list with a "does not exist" message
    assert _locks(ctx) == []


@pytest.mark.usefixtures("no_manifest_storage")
def test_the_locks_take_the_rows_of_one_table_in_pk_order(
    client: Client, superuser: RevelUser, series_pass: SeriesPass
) -> None:
    event, tier = cover(series_pass, "admin-order", days=4)
    TicketTier.objects.create(event=event, name="Second", price=Decimal("5.00"), currency="EUR")
    client.force_login(superuser)

    with CaptureQueriesContext(connection) as ctx:
        client.post(reverse("admin:events_event_change", args=[event.pk]), data={})

    tier_lock = next(sql for sql in _locks(ctx) if 'FROM "events_tickettier"' in sql)
    # Column 1 is the pk. The model's default ordering (event, display_order, name) would show its own columns here.
    assert tier_lock.startswith('SELECT "events_tickettier"."id" AS "pk" FROM'), tier_lock
    assert tier_lock.endswith('ORDER BY 1 ASC FOR NO KEY UPDATE OF "events_tickettier"'), tier_lock
