"""Calendar alignment, start month and a calendar plan's period unit are create-only on every save path."""

import typing as t
from decimal import Decimal

import pytest
from django.contrib.admin.sites import site as admin_site
from django.core.exceptions import ValidationError
from django.test.client import Client
from django.urls import reverse
from pytest_django.fixtures import Settings

from accounts.models import RevelUser
from events.models import MembershipSubscriptionPlan, MembershipTier, Organization

Plan = MembershipSubscriptionPlan


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Pro")


@pytest.fixture
def calendar_year_plan(tier: MembershipTier) -> Plan:
    return Plan.objects.create(
        tier=tier,
        name="Season",
        price=Decimal("120"),
        currency="EUR",
        period_unit=Plan.PeriodUnit.YEAR,
        period_alignment=Plan.PeriodAlignment.CALENDAR,
        calendar_start_month=9,
        payment_method=Plan.PaymentMethod.OFFLINE,
    )


@pytest.fixture
def rolling_plan(tier: MembershipTier) -> Plan:
    return Plan.objects.create(
        tier=tier,
        name="Rolling",
        price=Decimal("10"),
        currency="EUR",
        period_unit=Plan.PeriodUnit.MONTH,
        payment_method=Plan.PaymentMethod.OFFLINE,
    )


@pytest.mark.django_db
class TestCreateOnlyCalendarFields:
    def test_alignment_change_refused(self, rolling_plan: Plan) -> None:
        rolling_plan.period_alignment = Plan.PeriodAlignment.CALENDAR
        with pytest.raises(ValidationError) as exc_info:
            rolling_plan.save()
        assert "period_alignment" in exc_info.value.message_dict
        rolling_plan.refresh_from_db()
        assert rolling_plan.period_alignment == Plan.PeriodAlignment.ROLLING

    def test_start_month_change_refused(self, calendar_year_plan: Plan) -> None:
        calendar_year_plan.calendar_start_month = 3
        with pytest.raises(ValidationError) as exc_info:
            calendar_year_plan.save()
        assert "calendar_start_month" in exc_info.value.message_dict
        calendar_year_plan.refresh_from_db()
        assert calendar_year_plan.calendar_start_month == 9

    def test_calendar_unit_change_refused(self, calendar_year_plan: Plan) -> None:
        calendar_year_plan.period_unit = Plan.PeriodUnit.MONTH
        calendar_year_plan.calendar_start_month = 1  # keep the shape valid so the guard is what refuses
        with pytest.raises(ValidationError) as exc_info:
            calendar_year_plan.save(update_fields=["period_unit"])
        assert "period_unit" in exc_info.value.message_dict
        calendar_year_plan.refresh_from_db()
        assert calendar_year_plan.period_unit == Plan.PeriodUnit.YEAR

    def test_rolling_unit_change_still_allowed_without_subscribers(self, rolling_plan: Plan) -> None:
        rolling_plan.period_unit = Plan.PeriodUnit.YEAR
        rolling_plan.save()

    def test_update_fields_save_not_writing_them_still_works(self, calendar_year_plan: Plan) -> None:
        calendar_year_plan.calendar_start_month = 3  # in memory only, not written
        calendar_year_plan.name = "Renamed"
        calendar_year_plan.save(update_fields=["name"])
        assert Plan.objects.get(pk=calendar_year_plan.pk).calendar_start_month == 9

    def test_unchanged_full_save_allowed(self, calendar_year_plan: Plan) -> None:
        calendar_year_plan.name = "Renamed"
        calendar_year_plan.save()


@pytest.fixture
def no_manifest_storage(settings: Settings) -> None:
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.mark.django_db
def test_admin_change_form_shows_calendar_fields_readonly(
    no_manifest_storage: None, client: Client, superuser: RevelUser, calendar_year_plan: Plan
) -> None:
    client.force_login(superuser)
    response = client.get(reverse("admin:events_membershipsubscriptionplan_change", args=[calendar_year_plan.pk]))
    assert response.status_code == 200
    readonly = t.cast(list[str], response.context["adminform"].readonly_fields)
    assert {"period_alignment", "calendar_start_month"} <= set(readonly)
    assert "period_alignment" not in response.context["adminform"].form.fields
    assert admin_site.is_registered(Plan)


@pytest.mark.django_db
def test_admin_add_form_keeps_calendar_fields_editable(
    no_manifest_storage: None, client: Client, superuser: RevelUser
) -> None:
    client.force_login(superuser)
    response = client.get(reverse("admin:events_membershipsubscriptionplan_add"))
    assert response.status_code == 200
    assert "period_alignment" in response.context["adminform"].form.fields
