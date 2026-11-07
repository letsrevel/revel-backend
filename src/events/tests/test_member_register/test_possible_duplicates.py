"""Possible-duplicates view: unlinked entries paired with linked open STANDARD entries (#1319)."""

import typing as t
from datetime import timedelta

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import MembershipRecord, Organization

from .conftest import RecordFactory

pytestmark = pytest.mark.django_db


def _url(organization: Organization) -> str:
    return reverse("api:list_member_register_possible_duplicates", kwargs={"slug": organization.slug})


def _linked(
    make_record: RecordFactory, django_user_model: type[RevelUser], account_email: str, **extra: t.Any
) -> MembershipRecord:
    user = django_user_model.objects.create_user(
        username=account_email,
        email=account_email,
        password="pw",
        first_name=extra.pop("first", ""),
        last_name=extra.pop("last", ""),
    )
    return make_record(user=user, **extra)


def _pairs(response: t.Any) -> dict[tuple[str, str], list[str]]:
    assert response.status_code == 200, response.content
    return {(p["unlinked"]["id"], p["linked"]["id"]): p["reasons"] for p in response.json()}


def test_email_pair_uses_record_or_account_email(
    owner_client: Client,
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
) -> None:
    by_record = _linked(make_record, django_user_model, "acct1@example.com", email="shared1@example.com", full_name="A")
    by_account = _linked(make_record, django_user_model, "shared2@example.com", email="", full_name="B")
    u1 = make_record(email="SHARED1@example.com", full_name="Nobody One")
    u2 = make_record(email="shared2@example.com", full_name="Nobody Two")
    make_record(email="", full_name="Nobody Three")  # blank emails never pair

    pairs = _pairs(owner_client.get(_url(organization)))

    assert pairs == {(str(u1.pk), str(by_record.pk)): ["email"], (str(u2.pk), str(by_account.pk)): ["email"]}


def test_name_pair_is_accent_case_whitespace_insensitive(
    owner_client: Client,
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
) -> None:
    by_record = _linked(make_record, django_user_model, "a@example.com", email="a@example.com", full_name="José García")
    by_account = _linked(
        make_record,
        django_user_model,
        "b@example.com",
        email="b@example.com",
        full_name="Snapshot",
        first="Zoë",
        last="Ng",
    )
    u1 = make_record(email="", full_name="  JOSE   garcia ")
    u2 = make_record(email="", full_name="zoe NG")

    pairs = _pairs(owner_client.get(_url(organization)))

    assert pairs == {(str(u1.pk), str(by_record.pk)): ["name"], (str(u2.pk), str(by_account.pk)): ["name"]}


def test_claim_pair_and_combined_reasons(
    owner_client: Client,
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
) -> None:
    linked = _linked(make_record, django_user_model, "c@example.com", email="c@example.com", full_name="Claimer")
    claim_only = make_record(email="other@example.com", full_name="Other Name", duplicate_claimant=linked.user)
    everything = make_record(email="c@example.com", full_name="claimer", duplicate_claimant=linked.user)

    pairs = _pairs(owner_client.get(_url(organization)))

    assert pairs[(str(claim_only.pk), str(linked.pk))] == ["claim"]
    assert pairs[(str(everything.pk), str(linked.pk))] == ["email", "name", "claim"]


def test_closed_unlinked_pairs_but_merged_linked_and_closed_linked_do_not(
    owner_client: Client,
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
) -> None:
    linked = _linked(make_record, django_user_model, "d@example.com", email="d@example.com", full_name="Dee")
    closed_unlinked = make_record(
        email="d@example.com",
        full_name="x",
        valid_from=timezone.now() - timedelta(days=5),
        valid_until=timezone.now() - timedelta(days=1),
    )
    merged = make_record(email="d@example.com", full_name="y", merged_into=linked)
    other_linked = _linked(make_record, django_user_model, "e@example.com", email="e@example.com", full_name="Eve")
    # A closed linked record is not a "linked open STANDARD" counterpart.
    closed_linked = make_record(
        user=other_linked.user,
        email="f@example.com",
        full_name="Eve Closed",
        valid_from=timezone.now() - timedelta(days=5),
        valid_until=timezone.now() - timedelta(days=1),
    )
    make_record(email="f@example.com", full_name="z")

    pairs = _pairs(owner_client.get(_url(organization)))

    assert (str(closed_unlinked.pk), str(linked.pk)) in pairs
    assert not any(u == str(merged.pk) for u, _l in pairs)
    assert not any(lk == str(closed_linked.pk) for _u, lk in pairs)
    assert not any(u in (str(linked.pk), str(other_linked.pk)) for u, _l in pairs)


def test_cross_org_isolation(
    owner_client: Client,
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
) -> None:
    other = Organization.objects.create(
        name="Other",
        slug="other",
        owner=django_user_model.objects.create_user(username="oo", email="oo@x.io", password="p"),
    )
    foreign_user = django_user_model.objects.create_user(username="fu", email="same@example.com", password="p")
    MembershipRecord.objects.create(
        organization=other,
        user=foreign_user,
        full_name="Same",
        email="same@example.com",
        valid_from=timezone.now(),
        source=MembershipRecord.Source.MANUAL,
    )
    make_record(email="same@example.com", full_name="Same")

    assert _pairs(owner_client.get(_url(organization))) == {}


def test_permission_matrix(
    organization: Organization,
    owner_client: Client,
    outsider_client: Client,
) -> None:
    assert owner_client.get(_url(organization)).status_code == 200
    assert outsider_client.get(_url(organization)).status_code == 404  # org hidden from outsiders
    assert Client().get(_url(organization)).status_code == 401


def test_staff_with_manage_members_allowed(organization: Organization, staff_client: Client) -> None:
    assert staff_client.get(_url(organization)).status_code == 200


def test_staff_without_manage_members_forbidden(organization: Organization, plain_staff_client: Client) -> None:
    assert plain_staff_client.get(_url(organization)).status_code == 403
