"""Acquisition attribution stamped on POST /organizations/ (#1075).

Same contract as ticket purchase attribution (#922): the tags ride in the payload only,
malformed values are dropped rather than rejected, and no tags means a null column.
"""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Organization

pytestmark = pytest.mark.django_db


@pytest.fixture
def verified_client(nonmember_client: Client, nonmember_user: RevelUser) -> Client:
    nonmember_user.email_verified = True
    nonmember_user.save()
    return nonmember_client


def _create(client: Client, **extra: t.Any) -> Organization:
    payload = {"name": "Attributed Collective", "contact_email": "contact@attributed.org", **extra}
    response = client.post(reverse("api:create_organization"), data=payload, content_type="application/json")
    assert response.status_code == 201, response.content
    return Organization.objects.get(name="Attributed Collective")


def test_create_organization_stamps_attribution(verified_client: Client) -> None:
    """The four utm tags from the create-org page URL land on the organization verbatim."""
    tags = {
        "utm_source": "revel",
        "utm_medium": "landing",
        "utm_campaign": "eventbrite-alternative",
        "utm_content": "hero_cta",
    }

    org = _create(verified_client, attribution=tags)

    assert org.attribution == tags


def test_create_organization_sanitises_attribution(verified_client: Client) -> None:
    """Unknown keys and malformed values are dropped; a bad tag never fails the create."""
    org = _create(
        verified_client,
        attribution={
            "utm_source": "  newsletter  ",
            "utm_medium": "<script>",
            "utm_term": "ignored",
            "gclid": "ignored",
        },
    )

    assert org.attribution == {"utm_source": "newsletter"}


@pytest.mark.parametrize("extra", [{}, {"attribution": None}, {"attribution": {"utm_medium": "bad value!"}}])
def test_create_organization_without_usable_tags_stores_null(verified_client: Client, extra: dict[str, t.Any]) -> None:
    """Absent, null, or fully-sanitised-away tags all read as direct (null)."""
    org = _create(verified_client, **extra)

    assert org.attribution is None
