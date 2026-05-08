"""Tests for the public organization contact form endpoint and related schema behavior."""

from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import Organization, OrganizationContactMessage

pytestmark = pytest.mark.django_db


@pytest.fixture
def public_org_with_form_contact(organization: Organization) -> Organization:
    """Public org with contact_method=FORM and verified contact email."""
    organization.visibility = Organization.Visibility.PUBLIC
    organization.contact_email = "info@example.com"
    organization.contact_email_verified = True
    organization.contact_method = Organization.ContactMethod.FORM
    organization.save()
    return organization


@pytest.fixture
def verified_nonmember_client(nonmember_user: RevelUser) -> Client:
    """Authenticated client with verified email."""
    nonmember_user.email_verified = True
    nonmember_user.save()
    from ninja_jwt.tokens import RefreshToken

    refresh = RefreshToken.for_user(nonmember_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


# --- Resolver / schema tests ---


def test_public_schema_exposes_contact_email_only_when_method_is_email(
    client: Client, organization: Organization
) -> None:
    """Resolver returns contact_email when contact_method=EMAIL and verified."""
    organization.visibility = Organization.Visibility.PUBLIC
    organization.contact_email = "info@example.com"
    organization.contact_email_verified = True
    organization.contact_method = Organization.ContactMethod.EMAIL
    organization.save()

    url = reverse("api:get_organization", kwargs={"slug": organization.slug})
    response = client.get(url)

    assert response.status_code == 200
    body = response.json()
    assert body["contact_email"] == "info@example.com"
    assert body["contact_method"] == "email"
    assert "contact_email_verified" not in body


def test_public_schema_hides_contact_email_when_method_is_none(client: Client, organization: Organization) -> None:
    """Resolver returns None when contact_method=NONE, even with verified email."""
    organization.visibility = Organization.Visibility.PUBLIC
    organization.contact_email = "info@example.com"
    organization.contact_email_verified = True
    organization.contact_method = Organization.ContactMethod.NONE
    organization.save()

    url = reverse("api:get_organization", kwargs={"slug": organization.slug})
    response = client.get(url)

    assert response.status_code == 200
    body = response.json()
    assert body["contact_email"] is None
    assert body["contact_method"] == "none"
    assert "contact_email_verified" not in body


def test_public_schema_hides_contact_email_when_method_is_form(client: Client, organization: Organization) -> None:
    """Resolver returns None when contact_method=FORM (form is the only public channel)."""
    organization.visibility = Organization.Visibility.PUBLIC
    organization.contact_email = "info@example.com"
    organization.contact_email_verified = True
    organization.contact_method = Organization.ContactMethod.FORM
    organization.save()

    url = reverse("api:get_organization", kwargs={"slug": organization.slug})
    response = client.get(url)

    assert response.status_code == 200
    body = response.json()
    assert body["contact_email"] is None
    assert body["contact_method"] == "form"


def test_public_schema_hides_contact_email_when_email_unverified(client: Client, organization: Organization) -> None:
    """Defense-in-depth: even with contact_method=EMAIL, hide if email not verified.

    This state should not be reachable through the API (service + Model.clean()
    forbid it). We bypass the model's full_clean() with a raw .update() to verify
    the resolver still hides the email if the row is corrupted directly in the DB.
    """
    organization.visibility = Organization.Visibility.PUBLIC
    organization.contact_email = "info@example.com"
    organization.contact_email_verified = True
    organization.contact_method = Organization.ContactMethod.EMAIL
    organization.save()
    # Bypass full_clean to simulate a direct DB corruption (verified flag flipped off).
    Organization.objects.filter(pk=organization.pk).update(contact_email_verified=False)

    url = reverse("api:get_organization", kwargs={"slug": organization.slug})
    response = client.get(url)

    assert response.status_code == 200
    body = response.json()
    assert body["contact_email"] is None


# --- POST /contact endpoint tests ---


@patch("events.tasks.send_organization_contact_message_email.delay")
def test_contact_organization_success_201(
    mock_email: object,
    verified_nonmember_client: Client,
    nonmember_user: RevelUser,
    public_org_with_form_contact: Organization,
) -> None:
    """POSTing a valid payload persists a message and returns 201."""
    url = reverse("api:contact_organization", kwargs={"slug": public_org_with_form_contact.slug})
    payload = {"subject": "Question about events", "message": "Hi, I have a question."}

    response = verified_nonmember_client.post(url, data=payload, content_type="application/json")

    assert response.status_code == 201, response.json()
    msgs = OrganizationContactMessage.objects.filter(organization=public_org_with_form_contact)
    assert msgs.count() == 1
    msg = msgs.get()
    assert msg.sender_id == nonmember_user.id
    assert msg.sender_email_snapshot == nonmember_user.email
    assert msg.subject == "Question about events"
    assert msg.message == "Hi, I have a question."


def test_contact_organization_400_when_method_none(
    verified_nonmember_client: Client, organization: Organization
) -> None:
    """Org with contact_method=NONE rejects the contact form with 400."""
    organization.visibility = Organization.Visibility.PUBLIC
    organization.contact_email = "info@example.com"
    organization.contact_email_verified = True
    organization.contact_method = Organization.ContactMethod.NONE
    organization.save()

    url = reverse("api:contact_organization", kwargs={"slug": organization.slug})
    response = verified_nonmember_client.post(url, data={"message": "hi"}, content_type="application/json")

    assert response.status_code == 400
    assert OrganizationContactMessage.objects.count() == 0


def test_contact_organization_400_when_method_email(
    verified_nonmember_client: Client, organization: Organization
) -> None:
    """Org with contact_method=EMAIL rejects the contact form with 400."""
    organization.visibility = Organization.Visibility.PUBLIC
    organization.contact_email = "info@example.com"
    organization.contact_email_verified = True
    organization.contact_method = Organization.ContactMethod.EMAIL
    organization.save()

    url = reverse("api:contact_organization", kwargs={"slug": organization.slug})
    response = verified_nonmember_client.post(url, data={"message": "hi"}, content_type="application/json")

    assert response.status_code == 400
    assert OrganizationContactMessage.objects.count() == 0


def test_contact_organization_404_when_org_not_visible(
    verified_nonmember_client: Client, organization: Organization
) -> None:
    """Endpoint 404s if the org is not visible to the requester."""
    organization.visibility = Organization.Visibility.PRIVATE
    organization.contact_email = "info@example.com"
    organization.contact_email_verified = True
    organization.contact_method = Organization.ContactMethod.FORM
    organization.save()

    url = reverse("api:contact_organization", kwargs={"slug": organization.slug})
    response = verified_nonmember_client.post(url, data={"message": "hi"}, content_type="application/json")

    assert response.status_code == 404
    assert OrganizationContactMessage.objects.count() == 0


def test_contact_organization_requires_verified_email(
    nonmember_client: Client,
    nonmember_user: RevelUser,
    public_org_with_form_contact: Organization,
) -> None:
    """Endpoint refuses unverified-email users (auth=requires_verified_email=True)."""
    assert nonmember_user.email_verified is False
    url = reverse("api:contact_organization", kwargs={"slug": public_org_with_form_contact.slug})
    response = nonmember_client.post(url, data={"message": "hi"}, content_type="application/json")

    assert response.status_code in {401, 403}
    assert OrganizationContactMessage.objects.count() == 0


def test_contact_organization_requires_authentication(
    client: Client, public_org_with_form_contact: Organization
) -> None:
    """Anonymous clients are rejected."""
    url = reverse("api:contact_organization", kwargs={"slug": public_org_with_form_contact.slug})
    response = client.post(url, data={"message": "hi"}, content_type="application/json")

    assert response.status_code == 401
    assert OrganizationContactMessage.objects.count() == 0


def test_contact_organization_rejects_empty_message(
    verified_nonmember_client: Client, public_org_with_form_contact: Organization
) -> None:
    """A blank message body is rejected by schema validation."""
    url = reverse("api:contact_organization", kwargs={"slug": public_org_with_form_contact.slug})
    response = verified_nonmember_client.post(url, data={"message": ""}, content_type="application/json")
    assert response.status_code == 422  # pydantic validation error
