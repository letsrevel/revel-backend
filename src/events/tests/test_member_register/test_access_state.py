"""Access state (member status, live subscription) on register rows, the user_id filter and filter AND-ing."""

import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipRecord,
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
)

from .conftest import RecordFactory

pytestmark = pytest.mark.django_db

Status = OrganizationMember.MembershipStatus
SubStatus = MembershipSubscription.SubscriptionStatus


@pytest.fixture
def plan(link_tier: MembershipTier) -> MembershipSubscriptionPlan:
    """A monthly plan on the linking tier."""
    return MembershipSubscriptionPlan.objects.create(
        tier=link_tier,
        name="Monthly Gold",
        price=Decimal("10.00"),
        currency="EUR",
        period_unit=MembershipSubscriptionPlan.PeriodUnit.MONTH,
        period_count=1,
    )


def _list_url(organization: Organization) -> str:
    return reverse("api:list_member_register", kwargs={"slug": organization.slug})


def _user(django_user_model: t.Any, n: int) -> RevelUser:
    return t.cast(
        RevelUser, django_user_model.objects.create_user(username=f"as{n}", email=f"as{n}@example.com", password="pw")
    )


def _member(organization: Organization, tier: MembershipTier, user: RevelUser, status: str = Status.ACTIVE) -> None:
    OrganizationMember.objects.create(organization=organization, user=user, tier=tier, status=status)


class TestAccessState:
    def test_values_and_flat_query_count(
        self,
        owner_client: Client,
        organization: Organization,
        link_tier: MembershipTier,
        plan: MembershipSubscriptionPlan,
        make_record: RecordFactory,
        django_user_model: t.Any,
    ) -> None:
        def listing() -> tuple[dict[str, t.Any], int]:
            with CaptureQueriesContext(connection) as ctx:
                response = owner_client.get(_list_url(organization), {"page_size": 50})
            assert response.status_code == 200, response.content
            return {r["full_name"]: r for r in response.json()["results"]}, len(ctx)

        first = _user(django_user_model, 0)
        _member(organization, link_tier, first)
        MembershipRecord.objects.filter(user=first).update(full_name="row0")
        _, baseline = listing()

        period_end = timezone.now() + timedelta(days=20)
        for i, (status, sub_status, cancel) in enumerate(
            [
                (Status.PAUSED, SubStatus.ACTIVE, True),
                (Status.ACTIVE, None, False),
                (Status.ACTIVE, SubStatus.PAUSED, False),
                (Status.CANCELLED, None, False),
            ],
            start=1,
        ):
            user = _user(django_user_model, i)
            _member(organization, link_tier, user)
            MembershipRecord.objects.filter(user=user, valid_until__isnull=True).update(full_name=f"row{i}")
            if sub_status:
                MembershipSubscription.objects.create(
                    user=user,
                    plan=plan,
                    organization=organization,
                    status=sub_status,
                    current_period_end=period_end,
                    cancel_at_period_end=cancel,
                )
            # A terminal subscription never shows.
            MembershipSubscription.objects.bulk_create(  # bulk: the save signal would cancel the member
                [MembershipSubscription(user=user, plan=plan, organization=organization, status=SubStatus.CANCELLED)]
            )
            # Queryset update (no signals): subscription/record syncs would otherwise close the entry.
            OrganizationMember.objects.filter(organization=organization, user=user).update(status=status)
        closed_user = _user(django_user_model, 9)
        _member(organization, link_tier, closed_user)
        make_record(
            user=closed_user,
            full_name="closed",
            valid_from=timezone.now() - timedelta(days=9),
            valid_until=timezone.now() - timedelta(days=1),
        )
        temp_user = _user(django_user_model, 10)
        make_record(
            user=temp_user,
            full_name="temp",
            kind=MembershipRecord.Kind.TEMPORARY,
            valid_from=timezone.now() - timedelta(days=9),
            valid_until=timezone.now() - timedelta(days=1),
        )

        rows, count = listing()
        assert count == baseline
        assert rows["row0"]["member_status"] == "active" and rows["row0"]["subscription"] is None
        assert rows["row1"]["member_status"] == "paused"
        assert rows["row1"]["subscription"]["status"] == "active"
        assert rows["row1"]["subscription"]["plan_name"] == "Monthly Gold"
        assert rows["row1"]["subscription"]["cancel_at_period_end"] is True
        assert rows["row1"]["subscription"]["current_period_end"] is not None
        assert rows["row2"]["subscription"] is None
        assert rows["row3"]["subscription"]["status"] == "paused"
        assert rows["row4"]["member_status"] == "cancelled"
        for name in ("closed", "temp"):
            assert rows[name]["member_status"] is None and rows[name]["subscription"] is None

        detail = owner_client.get(
            reverse(
                "api:get_member_register_record",
                kwargs={"slug": organization.slug, "record_id": rows["row1"]["id"]},
            )
        ).json()
        assert detail["member_status"] == rows["row1"]["member_status"]
        assert detail["subscription"] == rows["row1"]["subscription"]

    def test_unlinked_has_no_access_state(
        self, owner_client: Client, organization: Organization, make_record: RecordFactory
    ) -> None:
        make_record(full_name="nobody")
        row = owner_client.get(_list_url(organization)).json()["results"][0]
        assert row["member_status"] is None and row["subscription"] is None


class TestFilters:
    def test_user_id_returns_all_periods_newest_first(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        claimant: RevelUser,
    ) -> None:
        now = timezone.now()
        old = make_record(user=claimant, valid_from=now - timedelta(days=90), valid_until=now - timedelta(days=60))
        temp = make_record(
            user=claimant,
            kind=MembershipRecord.Kind.TEMPORARY,
            valid_from=now - timedelta(days=30),
            valid_until=now - timedelta(days=20),
        )
        current = make_record(user=claimant, valid_from=now - timedelta(days=10))
        make_record(full_name="someone else", email="else@example.com")
        response = owner_client.get(_list_url(organization), {"user_id": str(claimant.pk)})
        assert response.status_code == 200
        assert [r["id"] for r in response.json()["results"]] == [str(current.pk), str(temp.pk), str(old.pk)]

    def test_user_of_another_org_is_an_empty_page(
        self, owner_client: Client, organization: Organization, django_user_model: t.Any, link_tier: MembershipTier
    ) -> None:
        other = Organization.objects.create(name="Other", slug="other-as", owner=organization.owner)
        outsider = _user(django_user_model, 20)
        MembershipRecord.objects.create(
            organization=other,
            user=outsider,
            full_name="x",
            valid_from=timezone.now(),
            source=MembershipRecord.Source.MANUAL,
        )
        response = owner_client.get(_list_url(organization), {"user_id": str(outsider.pk)})
        assert response.status_code == 200 and response.json()["results"] == []

    def test_is_open_and_kind_combine_with_and(
        self, owner_client: Client, organization: Organization, make_record: RecordFactory
    ) -> None:
        now = timezone.now()
        open_standard = make_record(full_name="open-standard", valid_from=now - timedelta(days=5))
        make_record(
            full_name="closed-standard", valid_from=now - timedelta(days=9), valid_until=now - timedelta(days=2)
        )
        make_record(
            full_name="open-temporary", kind=MembershipRecord.Kind.TEMPORARY, valid_from=now - timedelta(days=5)
        )
        response = owner_client.get(_list_url(organization), {"is_open": "true", "kind": "standard"})
        assert [r["id"] for r in response.json()["results"]] == [str(open_standard.pk)]
