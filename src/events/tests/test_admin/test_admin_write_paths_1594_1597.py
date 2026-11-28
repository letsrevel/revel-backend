"""The Django admin cannot write past the services that keep capacity and money consistent (#1594, #1597, #1619).

A ticket or a held series pass counts one place on its tier or pass. A direct admin add, or a change of its
tier, pass or status, would skip that count. A membership payment holds the fee split, and a customer profile
holds the saved payment methods of one person. So the admin does not add these rows, and it fixes their parents.
"""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.admin.sites import site
from django.contrib.auth.models import Permission
from django.db import models as dj_models
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


@pytest.fixture
def payment(organization: models.Organization, member_user: RevelUser) -> models.MembershipPayment:
    tier = models.MembershipTier.objects.create(organization=organization, name="Pro")
    plan = models.MembershipSubscriptionPlan.objects.create(
        tier=tier,
        name="Monthly",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit=models.MembershipSubscriptionPlan.PeriodUnit.MONTH,
        period_count=1,
    )
    subscription = models.MembershipSubscription.objects.create(
        user=member_user,
        plan=plan,
        organization=organization,
        status=models.MembershipSubscription.SubscriptionStatus.ACTIVE,
    )
    now = timezone.now()
    return models.MembershipPayment.objects.create(
        subscription=subscription,
        amount=Decimal("10.00"),
        currency="EUR",
        period_start=now,
        period_end=now + timedelta(days=30),
    )


@pytest.fixture
def profile(organization: models.Organization, member_user: RevelUser) -> models.CustomerProfile:
    return models.CustomerProfile.objects.create(
        user=member_user, organization=organization, stripe_customer_id="cus_1"
    )


def _request(rf: t.Any, superuser: RevelUser) -> t.Any:
    request = rf.get("/")
    request.user = superuser
    return request


@pytest.mark.parametrize(
    "model", [models.Ticket, models.HeldSeriesPass, models.MembershipPayment], ids=lambda m: m.__name__
)
def test_add_is_refused(rf: t.Any, superuser: RevelUser, model: type[dj_models.Model]) -> None:
    assert not site._registry[model].has_add_permission(_request(rf, superuser))


def test_ticket_inline_add_is_refused(rf: t.Any, superuser: RevelUser, event: models.Event) -> None:
    request = _request(rf, superuser)
    inlines = site._registry[models.Event].get_inline_instances(request, event)
    ticket_inline = next(i for i in inlines if i.model is models.Ticket)

    assert not ticket_inline.has_add_permission(request, event)


@pytest.mark.parametrize(
    ("model", "fixture", "fields"),
    [
        pytest.param(models.Ticket, "ticket", {"event", "tier", "status"}, id="ticket"),
        pytest.param(models.HeldSeriesPass, "held_pass", {"series_pass", "status"}, id="held-pass"),
        pytest.param(models.CustomerProfile, "profile", {"user", "organization"}, id="customer-profile"),
    ],
)
def test_the_parent_and_status_are_readonly_on_change(
    rf: t.Any,
    superuser: RevelUser,
    request: pytest.FixtureRequest,
    model: type[dj_models.Model],
    fixture: str,
    fields: set[str],
) -> None:
    obj = request.getfixturevalue(fixture)

    readonly = site._registry[model].get_readonly_fields(_request(rf, superuser), obj)

    assert fields <= set(readonly)


def test_ticket_inline_status_and_tier_are_readonly(rf: t.Any, superuser: RevelUser, event: models.Event) -> None:
    request = _request(rf, superuser)
    inlines = site._registry[models.Event].get_inline_instances(request, event)
    ticket_inline = next(i for i in inlines if i.model is models.Ticket)

    assert {"status", "tier"} <= set(ticket_inline.get_readonly_fields(request))


def test_customer_profile_owner_is_editable_on_add(rf: t.Any, superuser: RevelUser) -> None:
    readonly = site._registry[models.CustomerProfile].get_readonly_fields(_request(rf, superuser), None)

    assert not {"user", "organization"} & set(readonly)


def test_event_organization_is_disabled_on_change_and_editable_on_add(
    rf: t.Any, superuser: RevelUser, event: models.Event
) -> None:
    admin_obj = site._registry[models.Event]
    request = _request(rf, superuser)

    assert admin_obj.get_form(request, event, change=True).base_fields["organization"].disabled
    assert not admin_obj.get_form(request, None).base_fields["organization"].disabled


def test_a_posted_organization_is_ignored(
    rf: t.Any, superuser: RevelUser, event: models.Event, revel_user_factory: t.Any
) -> None:
    other = models.Organization.objects.create(name="Other", slug="other", owner=revel_user_factory())
    form_class = site._registry[models.Event].get_form(_request(rf, superuser), event, change=True)
    data = {**_initial(form_class, event), "organization": str(other.pk), **_split_datetimes(event)}

    form = form_class(data=data, instance=event)

    assert form.is_valid(), form.errors
    assert form.cleaned_data["organization"] == event.organization


def _split_datetimes(event: models.Event) -> dict[str, str]:
    """The admin renders ``start`` and ``end`` as split date and time widgets."""
    return {
        f"{name}_{i}": value
        for name in ("start", "end")
        for i, value in enumerate(timezone.localtime(getattr(event, name)).strftime("%Y-%m-%d %H:%M:%S").split(" "))
    }


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


def test_an_existing_payment_is_view_only(rf: t.Any, superuser: RevelUser, payment: models.MembershipPayment) -> None:
    admin_obj = site._registry[models.MembershipPayment]
    request = _request(rf, superuser)

    assert not admin_obj.has_change_permission(request, payment)
    assert admin_obj.has_change_permission(request)


@pytest.mark.usefixtures("no_manifest_storage")
def test_a_payment_change_post_changes_nothing(
    client: Client, superuser: RevelUser, payment: models.MembershipPayment
) -> None:
    client.force_login(superuser)
    url = reverse("admin:events_membershippayment_change", args=[payment.pk])

    before = payment.refund_amount
    assert client.get(url).status_code == 200

    assert client.post(url, {"refund_amount": "5.00"}).status_code == 403
    payment.refresh_from_db()
    assert payment.refund_amount == before


@pytest.mark.usefixtures("no_manifest_storage")
def test_a_profile_delete_page_is_refused_but_a_user_delete_cascades(
    client: Client, rf: t.Any, superuser: RevelUser, profile: models.CustomerProfile, member_user: RevelUser
) -> None:
    client.force_login(superuser)
    admin_obj = site._registry[models.CustomerProfile]

    response = client.get(reverse("admin:events_customerprofile_delete", args=[profile.pk]))
    assert response.status_code == 403
    assert "delete_selected" not in admin_obj.get_actions(_request(rf, superuser))

    member_user.delete()

    assert not models.CustomerProfile.objects.filter(pk=profile.pk).exists()


@pytest.mark.usefixtures("no_manifest_storage")
@pytest.mark.parametrize(
    ("model", "fixture"),
    [
        pytest.param(models.Ticket, "ticket", id="ticket"),
        pytest.param(models.HeldSeriesPass, "held_pass", id="held-pass"),
    ],
)
def test_a_view_only_staff_user_can_open_the_change_page(
    client: Client,
    revel_user_factory: t.Any,
    request: pytest.FixtureRequest,
    model: type[dj_models.Model],
    fixture: str,
) -> None:
    obj = request.getfixturevalue(fixture)
    staff = revel_user_factory(is_staff=True)
    opts = model._meta
    staff.user_permissions.add(
        Permission.objects.get(content_type__app_label=opts.app_label, codename=f"view_{opts.model_name}")
    )
    client.force_login(staff)

    response = client.get(reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[obj.pk]))

    assert response.status_code == 200
