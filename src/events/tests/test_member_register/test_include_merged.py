"""The ``include_merged`` register filter (merged entries are hidden by default)."""

from datetime import timedelta

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import MembershipRecord, Organization

from .conftest import RecordFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def merged(make_record: RecordFactory, claimant: RevelUser) -> dict[str, MembershipRecord]:
    now = timezone.now()
    survivor = make_record(full_name="survivor", user=claimant, valid_from=now - timedelta(days=3))
    absorbed = make_record(full_name="absorbed", merged_into=survivor, valid_from=now - timedelta(days=9))
    other = make_record(full_name="other", email="o@example.com", valid_from=now - timedelta(days=1))
    return {"survivor": survivor, "absorbed": absorbed, "other": other}


def _get(client: Client, organization: Organization, **params: str) -> dict[str, object]:
    response = client.get(reverse("api:list_member_register", kwargs={"slug": organization.slug}), params)
    assert response.status_code == 200, response.content
    return response.json()  # type: ignore[no-any-return]


def _names(client: Client, organization: Organization, **params: str) -> set[str]:
    return {r["full_name"] for r in _get(client, organization, **params)["results"]}  # type: ignore[attr-defined]


def test_default_hides_merged_and_count_reflects_it(
    owner_client: Client, organization: Organization, merged: dict[str, MembershipRecord]
) -> None:
    body = _get(owner_client, organization)
    assert body["count"] == 2
    assert _names(owner_client, organization) == {"survivor", "other"}


def test_include_merged_shows_absorbed(
    owner_client: Client, organization: Organization, merged: dict[str, MembershipRecord]
) -> None:
    body = _get(owner_client, organization, include_merged="true")
    assert body["count"] == 3
    assert _names(owner_client, organization, include_merged="true") == {"survivor", "absorbed", "other"}


def test_user_id_with_include_merged_adds_absorbed_entries(
    owner_client: Client, organization: Organization, merged: dict[str, MembershipRecord], claimant: RevelUser
) -> None:
    names = _names(owner_client, organization, user_id=str(claimant.pk), include_merged="true")
    assert names == {"survivor", "absorbed"}


def test_user_id_alone_hides_absorbed(
    owner_client: Client, organization: Organization, merged: dict[str, MembershipRecord], claimant: RevelUser
) -> None:
    assert _names(owner_client, organization, user_id=str(claimant.pk)) == {"survivor"}


def test_combines_with_kind_and_is_open(
    owner_client: Client,
    organization: Organization,
    make_record: RecordFactory,
    merged: dict[str, MembershipRecord],
) -> None:
    now = timezone.now()
    survivor = merged["survivor"]
    make_record(
        full_name="absorbed-closed",
        merged_into=survivor,
        valid_from=now - timedelta(days=20),
        valid_until=now - timedelta(days=10),
    )
    open_names = _names(owner_client, organization, include_merged="true", is_open="true", kind="standard")
    assert open_names == {"survivor", "absorbed", "other"}
    closed = _names(owner_client, organization, include_merged="true", is_open="false", kind="standard")
    assert closed == {"absorbed-closed"}
    assert _names(owner_client, organization, is_open="false") == set()
    assert _names(owner_client, organization, include_merged="true", kind="temporary") == set()
