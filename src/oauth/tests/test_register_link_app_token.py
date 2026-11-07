"""Approve-and-link of a register entry (#1319) needs a session, never an OAuth app token."""

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import MembershipRecord, MembershipTier, Organization, OrganizationMembershipRequest
from oauth.models import OAuthApplication
from oauth.tests.test_auth_class import make_access_token

pytestmark = pytest.mark.django_db


def _setup(
    organization: Organization, revel_user_factory: type | object
) -> tuple[OrganizationMembershipRequest, MembershipRecord]:
    tier = MembershipTier.objects.create(organization=organization, name="Gold")
    applicant: RevelUser = revel_user_factory()  # type: ignore[operator]
    application = OrganizationMembershipRequest.objects.create(organization=organization, user=applicant, tier=tier)
    record = MembershipRecord.objects.create(
        organization=organization,
        full_name="Pat",
        tier=tier,
        tier_name="Gold",
        valid_from=timezone.now(),
        source=MembershipRecord.Source.MANUAL,
    )
    return application, record


def test_app_token_cannot_link_but_can_approve_plainly(
    organization: Organization, oauth_app: OAuthApplication, revel_user_factory: type
) -> None:
    application, record = _setup(organization, revel_user_factory)
    client = Client(
        HTTP_AUTHORIZATION=f"Bearer {make_access_token(organization.owner, oauth_app, 'org:read org:members')}"
    )
    url = reverse("api:approve_membership_request", kwargs={"slug": organization.slug, "request_id": application.pk})

    refused = client.post(url, data={"link_record_id": str(record.pk)}, content_type="application/json")

    assert refused.status_code == 403, refused.content
    record.refresh_from_db()
    application.refresh_from_db()
    assert record.user is None
    assert application.status == OrganizationMembershipRequest.Status.PENDING

    allowed = client.post(url, data={}, content_type="application/json")
    assert allowed.status_code == 204, allowed.content


def test_session_can_link(organization: Organization, session_client: Client, revel_user_factory: type) -> None:
    application, record = _setup(organization, revel_user_factory)
    url = reverse("api:approve_membership_request", kwargs={"slug": organization.slug, "request_id": application.pk})

    response = session_client.post(url, data={"link_record_id": str(record.pk)}, content_type="application/json")

    assert response.status_code == 204, response.content
    record.refresh_from_db()
    assert record.user == application.user
