"""The admin edit guard refuses a save that would revert a value a service changed after the page opened.

The change page keeps the value it showed in a hidden ``initial-`` input. On save, the admin compares that value
with the stored one. A different stored value means a service changed the row, so the save is refused.
Issues #1594, #1597 and #1619.
"""

import json
import re
import typing as t
from datetime import timedelta
from decimal import Decimal
from html.parser import HTMLParser

import pytest
from django.contrib.admin.sites import site
from django.db import models as dj_models
from django.db.models import F
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events import models

pytestmark = pytest.mark.django_db


@pytest.fixture
def no_manifest_storage(settings: t.Any) -> None:
    """The change page renders unfold's static files, which have no manifest in tests."""
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def held_pass(member_user: RevelUser, event_series: models.EventSeries) -> models.HeldSeriesPass:
    series_pass = models.SeriesPass.objects.create(
        event_series=event_series, name="Season", price=Decimal("50.00"), pro_rata_discount=Decimal("5.00")
    )
    return models.HeldSeriesPass.objects.create(series_pass=series_pass, user=member_user, price_paid=Decimal("50.00"))


def _request(rf: t.Any, superuser: RevelUser) -> t.Any:
    request = rf.get("/")
    request.user = superuser
    return request


@pytest.mark.usefixtures("no_manifest_storage")
def test_a_stale_tier_change_post_keeps_the_sold_count(
    client: Client, superuser: RevelUser, event_ticket_tier: models.TicketTier
) -> None:
    client.force_login(superuser)
    url = reverse("admin:events_tickettier_change", args=[event_ticket_tier.pk])
    response = client.get(url)
    assert response.status_code == 200
    form = response.context["adminform"].form
    data = {k: v for k, v in _initial(type(form), event_ticket_tier).items() if k != "quantity_sold"}
    data["quantity_sold"] = "0"  # the stale value shown when the page opened

    models.TicketTier.objects.filter(pk=event_ticket_tier.pk).update(quantity_sold=F("quantity_sold") + 3)
    response = client.post(url, {**data, "name": "Renamed"})

    assert response.status_code == 302
    event_ticket_tier.refresh_from_db()
    assert event_ticket_tier.quantity_sold == 3
    assert event_ticket_tier.name == "Renamed"


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
            else (str(value.pk) if hasattr(value, "pk") else json.dumps(value) if isinstance(value, dict) else value)
        )
        if field.show_hidden_initial:  # the value of the GET, which the edit guard compares with the stored one
            data[f"initial-{name}"] = data[name]
    return data


def _prefixed(prefix: str, index: int, key: str) -> str:
    """The POST key of a formset field; the hidden initial value keeps its ``initial-`` first."""
    if key.startswith("initial-"):
        return f"initial-{prefix}-{index}-{key.removeprefix('initial-')}"
    return f"{prefix}-{index}-{key}"


def _inline_formset(
    rf: t.Any, superuser: RevelUser, event: models.Event, model: type[dj_models.Model], edit: dict[str, t.Any]
) -> t.Any:
    """A bound inline formset of ``event`` with the first row edited by ``edit``."""
    request = _request(rf, superuser)
    inline = next(i for i in site._registry[models.Event].get_inline_instances(request, event) if i.model is model)
    formset_class = inline.get_formset(request, event)
    prefix = formset_class.get_default_prefix()
    rows = list(formset_class(instance=event, prefix=prefix).get_queryset())
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(rows)),
        f"{prefix}-INITIAL_FORMS": str(len(rows)),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for i, row in enumerate(rows):
        form_class = formset_class(instance=event, prefix=prefix).form
        initial = _initial(form_class, row)
        initial["id"] = str(row.pk)
        initial["event"] = str(event.pk)
        posted = {**initial, **(edit if i == 0 else {})}
        data.update({_prefixed(prefix, i, k): v for k, v in posted.items()})
    return formset_class(data=data, instance=event, prefix=prefix)


STALE = "This value changed since you opened the page"


@pytest.fixture
def discount_code(organization: models.Organization) -> models.DiscountCode:
    return models.DiscountCode.objects.create(
        code="STALE",
        organization=organization,
        discount_type=models.DiscountCode.DiscountType.PERCENTAGE,
        discount_value=Decimal("10.00"),
        max_uses=10,
    )


def _change_form_after_service_write(
    rf: t.Any,
    superuser: RevelUser,
    obj: t.Any,
    edit: dict[str, t.Any],
    service_write: t.Callable[[], object],
) -> t.Any:
    """Build the POST data from the page of the GET, let a service write the row, then validate the POST."""
    model_admin = site._registry[type(obj)]
    form_class = model_admin.get_form(_request(rf, superuser), obj, change=True)
    data = {**_initial(form_class, obj), **edit}
    service_write()
    form = form_class(data=data, instance=type(obj).objects.get(pk=obj.pk))
    form.is_valid()
    return form


@pytest.mark.usefixtures("no_manifest_storage")
def test_the_change_page_renders_the_hidden_initial_values(
    client: Client, superuser: RevelUser, discount_code: models.DiscountCode
) -> None:
    client.force_login(superuser)

    html = client.get(reverse("admin:events_discountcode_change", args=[discount_code.pk])).content.decode()

    assert 'name="initial-is_active"' in html
    assert 'name="initial-max_uses"' in html


@pytest.mark.usefixtures("no_manifest_storage")
def test_the_event_change_page_renders_the_hidden_initial_values_of_a_tier_row(
    client: Client, superuser: RevelUser, event: models.Event, event_ticket_tier: models.TicketTier
) -> None:
    client.force_login(superuser)

    html = client.get(reverse("admin:events_event_change", args=[event.pk])).content.decode()

    assert 'name="initial-ticket_tiers-0-name"' in html


class _FormParser(HTMLParser):
    """Collect what a browser would submit for the admin change form: (name, value) pairs in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_form = False
        self.pairs: list[tuple[str, str]] = []
        self._textarea: str | None = None
        self._textarea_text = ""
        self._select: str | None = None
        self._multiple = False
        self._selected: list[str] = []
        self._first: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "form" and a.get("id", "").endswith("_form"):
            self.in_form = True
        name = a.get("name", "")
        if tag == "option" and self._select:
            value = a.get("value", "")
            self._first = value if self._first is None else self._first
            if "selected" in a:
                self._selected.append(value)
        if not self.in_form or not name or "disabled" in a or "__prefix__" in name:
            return
        if tag == "input":
            kind = a.get("type", "text").lower()
            if kind in ("submit", "button", "file", "image", "reset"):
                return
            if kind in ("checkbox", "radio") and "checked" not in a:
                return
            self.pairs.append((name, a.get("value", "on" if kind == "checkbox" else "")))
        elif tag == "textarea":
            self._textarea, self._textarea_text = name, ""
        elif tag == "select":
            self._select, self._multiple = name, "multiple" in a
            self._selected, self._first = [], None

    def handle_data(self, data: str) -> None:
        if self._textarea is not None:
            self._textarea_text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.in_form = False
        elif tag == "textarea" and self._textarea is not None:
            # The HTML parser of a browser drops the first newline of a textarea.
            self.pairs.append((self._textarea, self._textarea_text.removeprefix("\n")))
            self._textarea = None
        elif tag == "select" and self._select:
            if self._selected:
                values = self._selected if self._multiple else self._selected[-1:]
            else:
                values = [] if self._multiple or self._first is None else [self._first]
            self.pairs.extend((self._select, v) for v in values)
            self._select = None


def _round_trip(
    client: Client,
    url: str,
    normalize_newlines: bool = True,
    edit: dict[str, str] | None = None,
    before_post: t.Callable[[], object] | None = None,
    drop: t.Container[str] = (),
) -> t.Any:
    """GET the change page, submit its values as a browser does, and return the response of the POST.

    A browser changes every line break of every entry (hidden inputs too) to CRLF. ``edit`` replaces entries,
    ``drop`` removes entries, and ``before_post`` runs between the GET and the POST (a service write).
    """
    parser = _FormParser()
    parser.feed(client.get(url).content.decode())
    data: dict[str, list[str]] = {}
    for name, value in parser.pairs:
        if name in drop:
            continue
        if normalize_newlines:
            value = re.sub(r"\r\n|\r|\n", "\r\n", value)
        data.setdefault(name, []).append(value)
    data["_save"] = ["Save"]
    for name, value in (edit or {}).items():
        data[name] = [value]
    if before_post:
        before_post()
    return client.post(url, data)


@pytest.mark.usefixtures("no_manifest_storage")
def test_a_stale_discount_code_save_is_refused_and_keeps_the_service_value(
    client: Client, superuser: RevelUser, discount_code: models.DiscountCode
) -> None:
    client.force_login(superuser)

    response = _round_trip(
        client,
        reverse("admin:events_discountcode_change", args=[discount_code.pk]),
        edit={"max_uses": "20"},
        before_post=lambda: models.DiscountCode.objects.filter(pk=discount_code.pk).update(is_active=False),
    )

    assert response.status_code == 200
    assert STALE in response.content.decode()
    discount_code.refresh_from_db()
    assert discount_code.is_active is False
    assert discount_code.max_uses == 10


@pytest.mark.usefixtures("no_manifest_storage")
def test_a_stale_ticket_save_is_refused_and_keeps_the_service_value(
    client: Client,
    superuser: RevelUser,
    event: models.Event,
    event_ticket_tier: models.TicketTier,
    member_user: RevelUser,
) -> None:
    client.force_login(superuser)
    ticket = models.Ticket.objects.create(
        event=event, tier=event_ticket_tier, user=member_user, status=models.Ticket.TicketStatus.ACTIVE
    )

    response = _round_trip(
        client,
        reverse("admin:events_ticket_change", args=[ticket.pk]),
        edit={"guest_name": "Edited"},
        before_post=lambda: models.Ticket.objects.filter(pk=ticket.pk).update(guest_name="Service"),
    )

    assert response.status_code == 200
    assert STALE in response.content.decode()
    ticket.refresh_from_db()
    assert ticket.guest_name == "Service"


@pytest.fixture
def ticket(event: models.Event, event_ticket_tier: models.TicketTier, member_user: RevelUser) -> models.Ticket:
    return models.Ticket.objects.create(
        event=event, tier=event_ticket_tier, user=member_user, status=models.Ticket.TicketStatus.ACTIVE
    )


@pytest.mark.usefixtures("no_manifest_storage")
@pytest.mark.parametrize("normalize_newlines", [False, True], ids=["lf", "browser-crlf"])
@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (models.Announcement, "title", "Hi "),
        (models.Announcement, "body", "Hello\nWorld"),
        (models.TicketTier, "description", "line1\nline2"),
        (models.TicketTier, "description", "  padded  "),
        (models.Ticket, "guest_name", "Guest "),
    ],
    ids=["announcement-title", "announcement-body", "tier-lf", "tier-padded", "ticket-guest"],
)
def test_an_unedited_save_of_text_with_a_line_break_or_edge_space_is_not_stale(
    client: Client,
    superuser: RevelUser,
    organization: models.Organization,
    event_ticket_tier: models.TicketTier,
    ticket: models.Ticket,
    model: type[models.Announcement] | type[models.TicketTier] | type[models.Ticket],
    field: str,
    value: str,
    normalize_newlines: bool,
) -> None:
    client.force_login(superuser)
    if model is models.Announcement:
        obj: t.Any = models.Announcement.objects.create(
            organization=organization, title="Hi", body="Hello", created_by=superuser
        )
    else:
        obj = {models.TicketTier: event_ticket_tier, models.Ticket: ticket}[model]
    model.objects.filter(pk=obj.pk).update(**{field: value})
    url = reverse(f"admin:events_{model._meta.model_name}_change", args=[obj.pk])

    response = _round_trip(client, url, normalize_newlines)

    assert response.status_code == 302, response.context["adminform"].form.errors


@pytest.mark.usefixtures("no_manifest_storage")
def test_an_unedited_save_of_each_guarded_admin_is_not_stale(
    client: Client,
    superuser: RevelUser,
    organization: models.Organization,
    event: models.Event,
    event_ticket_tier: models.TicketTier,
    ticket: models.Ticket,
    held_pass: models.HeldSeriesPass,
    discount_code: models.DiscountCode,
) -> None:
    client.force_login(superuser)
    discount_code.events.add(event)
    discount_code.tiers.add(event_ticket_tier)
    models.DiscountCode.objects.filter(pk=discount_code.pk).update(
        valid_from=timezone.now().replace(microsecond=123456), valid_until=timezone.now() + timedelta(days=3)
    )
    announcement = models.Announcement.objects.create(
        organization=organization, title="Hi", body="Hello", created_by=superuser
    )
    urls = [
        reverse("admin:events_tickettier_change", args=[event_ticket_tier.pk]),
        reverse("admin:events_ticket_change", args=[ticket.pk]),
        reverse("admin:events_seriespass_change", args=[held_pass.series_pass.pk]),
        reverse("admin:events_heldseriespass_change", args=[held_pass.pk]),
        reverse("admin:events_discountcode_change", args=[discount_code.pk]),
        reverse("admin:events_announcement_change", args=[announcement.pk]),
        reverse("admin:events_event_change", args=[event.pk]),
    ]

    assert [_round_trip(client, url).status_code for url in urls] == [302] * len(urls)


def test_a_missing_hidden_initial_is_refused(
    rf: t.Any, superuser: RevelUser, discount_code: models.DiscountCode
) -> None:
    form_class = site._registry[models.DiscountCode].get_form(_request(rf, superuser), discount_code, change=True)
    data = {k: v for k, v in _initial(form_class, discount_code).items() if k != "initial-max_uses"}

    form = form_class(data=data, instance=discount_code)

    assert not form.is_valid()
    assert STALE in str(form.errors["max_uses"])


def test_a_missing_hidden_initial_of_an_empty_value_is_not_stale(
    rf: t.Any, superuser: RevelUser, discount_code: models.DiscountCode
) -> None:
    models.DiscountCode.objects.filter(pk=discount_code.pk).update(is_active=False)
    discount_code.refresh_from_db()
    form_class = site._registry[models.DiscountCode].get_form(_request(rf, superuser), discount_code, change=True)
    data = {k: v for k, v in _initial(form_class, discount_code).items() if k != "initial-is_active"}

    form = form_class(data=data, instance=discount_code)

    assert form.is_valid(), form.errors


def test_an_untouched_change_form_saves(rf: t.Any, superuser: RevelUser, discount_code: models.DiscountCode) -> None:
    form = _change_form_after_service_write(rf, superuser, discount_code, {"max_uses": "20"}, lambda: None)

    assert form.is_valid(), form.errors
    form.save()
    discount_code.refresh_from_db()
    assert discount_code.max_uses == 20


def test_a_refused_form_keeps_the_hidden_initial_of_the_page(
    rf: t.Any, superuser: RevelUser, discount_code: models.DiscountCode
) -> None:
    form = _change_form_after_service_write(
        rf,
        superuser,
        discount_code,
        {"max_uses": "20"},
        lambda: models.DiscountCode.objects.filter(pk=discount_code.pk).update(is_active=False),
    )

    hidden = str(form["is_active"].as_hidden(only_initial=True))

    assert 'name="initial-is_active"' in hidden
    assert 'value="True"' in hidden


@pytest.fixture
def two_tiers(event: models.Event, event_ticket_tier: models.TicketTier) -> tuple[models.TicketTier, models.TicketTier]:
    other = models.TicketTier.objects.create(
        event=event, name="Other", price=Decimal("5.00"), payment_method=models.TicketTier.PaymentMethod.ONLINE
    )
    return event_ticket_tier, other


def test_an_untouched_inline_row_that_a_service_changed_is_not_saved(
    rf: t.Any, superuser: RevelUser, event: models.Event, two_tiers: tuple[models.TicketTier, models.TicketTier]
) -> None:
    formset = _inline_formset(rf, superuser, event, models.TicketTier, {"name": "Renamed", "category_prices": "{}"})
    first, second = formset.get_queryset()[:2]
    models.TicketTier.objects.filter(pk=second.pk).update(name="Service")

    assert formset.is_valid(), formset.errors
    formset.save()

    assert models.TicketTier.objects.get(pk=first.pk).name == "Renamed"
    assert models.TicketTier.objects.get(pk=second.pk).name == "Service"


def test_an_inline_row_that_a_service_changed_and_the_admin_edited_is_refused(
    rf: t.Any, superuser: RevelUser, event: models.Event, two_tiers: tuple[models.TicketTier, models.TicketTier]
) -> None:
    formset = _inline_formset(
        rf, superuser, event, models.TicketTier, {"description": "Edited", "category_prices": "{}"}
    )
    first, _ = formset.get_queryset()[:2]
    models.TicketTier.objects.filter(pk=first.pk).update(name="Service")

    assert not formset.is_valid()
    assert STALE in str(formset.forms[0].errors["name"])
    assert models.TicketTier.objects.get(pk=first.pk).name == "Service"
